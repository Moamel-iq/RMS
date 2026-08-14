# Task 2.0 — Procurement and Accounts Payable domain specification

- **Status:** Specification only. Task 2.0 creates no models, migrations,
  services, API or UI. Implementation begins at Task 2.1.
- **Implemented through Task 2.13** (supplier returns). §10's return half is
  live: a distinct `RETURN_OUT` movement (PRC-047), average-cost valuation with
  full depletion (PRC-048), the kernel's negative-stock refusal (PRC-050).
  Three amendments to this document, recorded in ADR-022:
  **(1)** §10 gains `SupplierReturnLine` — the header sketch alone has no
  quantity, no amount and no double-return guard.
  **(2)** PRC-049's variance is recognised at the **credit note**, not at the
  return; the return posts `Dr SUPPLIER_RETURN_CLEARING / Cr
  INVENTORY_CONTROL` only, under two new §15 roles: `SUPPLIER_RETURN_CLEARING`
  (`8-01-04-001`, CLEARING) and `PURCHASE_RETURN_VARIANCE` (`7-09-04-001`,
  OTHER, seeded and deliberately unmapped until Task 2.14).
  **(3)** §13 gains `view_supplier_return`, `create_supplier_return` and
  `reverse_supplier_return` beside the lone `post_supplier_return`, all
  warehouse-scoped; the storekeeper posts and cannot reverse.
- **Implemented through Task 2.12** (price variance accounting). §9's posting
  is live in full: GRNI clears at the delivered value, the payable takes the
  invoiced value, and the difference is parked. Two amendments to this
  document, both recorded in ADR-022 and ADR-023:
  **(1)** §15's `PURCHASE_PRICE_VARIANCE | 5-02-01-001` is **superseded** by
  `8-01-03-001`, class CLEARING. Class 5 sets `requires_cost_center` and a
  supplier invoice has no cost centre to supply; ADR-022 separately rejects
  booking a purchasing outcome as cost of sales.
  **(2)** PRC-044's on-hand revaluation is **DEFERRED and NOT ELECTED** — its
  permission, source identity, allocation policy and reversal rules are
  undefined here, and inventing them was refused.
- **Implemented through Task 2.11** (three-way matching). §9's allocation is
  live as `PurchaseMatchAllocation` beneath an explicit `PurchaseMatch` header,
  and its single `matched_value` is split into `receipt_allocated_value`,
  `invoice_allocated_value` and their difference, because §9's own posting
  formula needs both sides and cannot be computed from one figure. The variance
  is **information** at this task: computed, stored, displayed and summed, but
  posted by nothing. Matching moves no stock, clears no GRNI and leaves the
  invoice `APPROVED`; §9's journal is Task 2.12's to write.
- **Implemented through Task 2.10** (supplier invoices and the payable).
  §15's new account roles are seeded one at a time by the task that posts to
  them: `SUPPLIER_PAYABLE` arrived with 2.10; `PURCHASE_PRICE_VARIANCE`,
  `SUPPLIER_ADVANCE` and the two payment-source roles are still unseeded, and
  deliberately so — a role with no posting rule behind it is a mapping an
  accounting manager can be asked to fill in for a workflow that does not
  exist. §9's invoice posting is **partially** implemented: the direct-account
  route is live, and the GRNI/variance route waits for the match allocation
  2.11 defines. There is no `UNRECEIVED_INVENTORY_CLEARING` role in §15 and
  none was invented, so posting an invoice that arrives before its goods is
  not supported.
- **Date:** 2026-08-11
- **Branch:** `phase/2-procurement`, from tag `phase-1-inventory-complete`
  (`e49da77`)
- **Related:** ADR-006 – ADR-021, `docs/invariants/inventory-invariants.md`,
  `docs/invariants/procurement-invariants.md`,
  `docs/tasks/phase-2-task-breakdown.md`, proposed ADR-022 and ADR-023

Procurement is the first real source of stock quantity and cost. Everything
Phase 3 will claim about a recipe's cost rests on what a receipt said an
ingredient was worth, so the contract has to be settled before any of it is
written.

## 0. Source material, and what is missing from it

| Source | Where | Used for |
|---|---|---|
| Revised architecture plan | `System/files/Khan_Mandi_..._Revised_Architecture_and_Claude_Code_Plan.txt` | The lifecycle separation (§2), Phase 2 scope, posting-policy direction |
| ADR-016 | `docs/decisions/` | Permission-plus-scope; 404 vs 403 |
| ADR-017 | `docs/decisions/` | Source identity, idempotency, fingerprints |
| ADR-018 | `docs/decisions/` | Moving weighted average, the full-depletion rule, the valuation key |
| ADR-019 | `docs/decisions/` | Account roles; why no posting service names an account |
| ADR-020 | `docs/decisions/` | Cross-branch clearing, in-transit ownership |
| Kernel invariants | `docs/specs/accounting-kernel-invariants.md` | What a procurement posting must not break |
| Inventory invariants | `docs/invariants/inventory-invariants.md` | The thirty-five that already hold |
| Phase 1 code | `apps/inventory`, `apps/accounting`, `apps/organizations` | The actual kernel, not a summary of it |

**There is still no SRS.** `docs/requirements/SRS.md` is referenced by
`CLAUDE.md` and has never been added; Task 1.0 §0 recorded the same absence and
searched for it. Nothing has changed. The correct statement is therefore the
one Task 1.0 established:

> No contradiction was found against the architecture plan, the approved ADRs,
> and the current implementation. Reconciliation against the authoritative SRS
> has not been completed because the SRS is absent from the repository.

Every `PRC-*` requirement below traces to the architecture plan, to an ADR, or
to the Phase 1 implementation. They are **repository-local identifiers** until
an authoritative SRS arrives and is mapped.

**Iraqi VAT and withholding are out of scope, and not by oversight.** The
source documents state no tax treatment for restaurant purchases, and §16 of
this specification records why inventing one would be worse than omitting it.

---

## 1. The seven events, and why they are seven models

The architecture plan is unusually direct here:

> Do not model a purchase as one editable form that simultaneously means
> ordering, receiving, invoicing, and paying. These are separate business
> events and often happen on different dates.

Meat arrives Sunday. The invoice is approved Tuesday, for a different amount.
Payment leaves the cashbox at the end of the month, covering three invoices and
part of a fourth. Those are four dates, three documents and two ledgers, and a
single mutable "purchase" row can represent at most one of them honestly.

| Event | Stock effect | Accounting effect | Owns |
|---|---|---|---|
| Purchase request | none | none | What a branch says it needs |
| Supplier quotation | none | none | What a supplier says it costs |
| Purchase order | none | none | What was agreed, and at what price |
| Goods receipt | **increase, accepted quantity only** | Dr Inventory control / Cr GRNI | What physically arrived |
| Supplier invoice | none | Dr GRNI + variance / Cr Supplier payable | What the supplier charged |
| Supplier return | **decrease** | Dr GRNI or payable / Cr Inventory control | What physically went back |
| Supplier credit note | none | Dr Supplier payable / Cr GRNI or variance | What the supplier agreed to refund |
| Supplier payment | none | Dr Supplier payable / Cr Cash or bank | What actually left |

Read the two "none" columns carefully, because they are the load-bearing part:

- **A purchase order moves nothing.** It creates no stock and no payable. An
  order placed and never delivered leaves the books exactly as it found them.
- **A supplier invoice touches no stock.** It confirms what is owed for goods
  the receipt already brought in. An invoice that changes the price of stock
  still on hand adjusts *value* through the variance path in §9 — it never
  re-posts a quantity.
- **A supplier return and a credit note are different events**, and either can
  happen without the other. Goods can go back before anyone agrees a figure;
  a supplier can concede a price without anything moving.

**PRC-001.** No procurement model may combine two of these events in one row.

---

## 2. Supplier ownership and identity

`Supplier` is **organization-scoped**, exactly like `InventoryItem`. Branches
buy from the organization's suppliers; a branch does not own a supplier list.
This follows ADR-007 and the plan's own note about a "shared supplier master,
if centrally managed".

```
Supplier
    organization        FK PROTECT
    code                canonical strip().upper(), unique per organization
    name_ar             required
    name_en             optional
    contact_name        optional
    phone               optional, canonicalised by apps/users/phone.py
    email               optional
    address             optional
    payment_terms_days  PositiveSmallInteger, default 0 (cash on delivery)
    credit_limit        Decimal(18, 3) or NULL for "no stated limit"
    notes               optional
    is_active           bool
    public_id           UUID, immutable, the source-document identity
```

**PRC-002.** A supplier code is canonical uppercase, unique per organization,
and an archived code stays reserved forever — the same rule as item codes
(inventory invariant 23), for the same reason: a reused code silently rewrites
history in every report that groups by it.

**PRC-003.** `Supplier` carries **no balance field**. The balance owed to a
supplier is derived from posted invoices, credit notes and payment allocations,
every time it is asked for. This is not an optimisation decision that can be
revisited under load: a stored balance is a second source of truth that drifts,
and the architecture plan names it explicitly —

> supplier balances … are derived from posted ledger entries. They must not be
> maintained as unrelated mutable numbers.

**PRC-004.** A supplier is archived, never deleted. `PROTECT` on every FK.

### Payment terms are a snapshot, not a lookup

`payment_terms_days` on the supplier is the **default for new documents**. Each
purchase order and each invoice stores the terms that applied to it. Changing a
supplier's terms in March must not silently restate January's due dates, and a
report that recomputes aging from today's master data is answering a different
question from the one it is being asked.

---

## 3. Supplier item catalogue

```
SupplierItem
    supplier            FK PROTECT
    item                FK PROTECT to inventory.InventoryItem
    supplier_sku        the supplier's own code, free text
    package_unit        FK to inventory.PackageUnit, nullable
    last_quoted_price   Decimal(18, 6), nullable — informational only
    lead_time_days      PositiveSmallInteger, nullable
    minimum_order_qty   Decimal(18, 3), nullable, in the package unit
    is_preferred        bool
    effective_from      date
    effective_to        date, nullable
```

**PRC-005.** A catalogue price is **informational**. It never values stock, and
no posting service reads it. Inventory value comes from a receipt line's price
snapshot and nowhere else. The catalogue answers "what do we usually pay"; the
receipt answers "what did this cost".

**PRC-006.** At most one `is_preferred` row per `(supplier, item)` effective at
a given date, and at most one preferred **supplier** per item — a partial
unique index, the same shape as inventory invariant 27.

**PRC-007.** Effective periods for one `(supplier, item)` may not overlap —
`EXCLUDE USING gist`, as with `ItemPackageConversion` (invariant 28). A
catalogue row that has been referenced by a posted document is versioned, never
edited.

**PRC-008.** The package unit on a catalogue row must be one the *item* has a
conversion for. A supplier cannot invent a package the item does not know how
to convert to base, because the receipt would then have no factor to snapshot.
`FIXED` and `VARIABLE` packages are both admissible; §7 covers what changes.

---

## 4. Purchase requests

```
PurchaseRequest                     PurchaseRequestLine
    organization  FK                    request      FK CASCADE
    branch        FK                    item         FK PROTECT
    number        per-organization      package_unit FK, nullable
    requester     FK to User            entered_quantity   Decimal(18, 3)
    required_date date                  conversion / factor / version snapshot
    purpose       text                  base_quantity      Decimal(18, 3)
    warehouse     FK, the destination   note
    location      FK, nullable          line_uid     stable per line
    status        DRAFT → SUBMITTED
                  → APPROVED | REJECTED | CANCELLED
    submitted_by / submitted_at
    decided_by / decided_at / decision_reason
```

**PRC-009.** A purchase request has **no inventory effect and no accounting
effect**, in any status. It is a statement of need.

**PRC-010.** Maker-checker: `decided_by != submitted_by`, enforced by a
database `CheckConstraint`, not only in the service — the same treatment stock
counts got (inventory invariant 32). A service check is a promise; a constraint
is a guarantee that survives a data fix applied at 2 a.m. through a shell.

**PRC-011.** Only `DRAFT` is editable. `SUBMITTED` freezes the lines. An
approved request that turns out to be wrong is **cancelled and replaced**, not
edited — the correction pattern the whole system already uses.

**PRC-012.** The base quantity is snapshotted at submission using the
conversion effective on the branch business date, with factor and version
stored on the line. A conversion revised later does not restate what was asked
for. This is inventory invariant 3 applied one document earlier.

---

## 5. Quotations and comparison

```
SupplierQuotation                   SupplierQuotationLine
    organization  FK                    quotation    FK CASCADE
    supplier      FK PROTECT             item         FK PROTECT
    request       FK, nullable           package_unit FK, nullable
    number        per-organization       entered_quantity / factor / base_quantity
    quoted_at     date                   unit_price   Decimal(18, 6) per entered unit
    valid_until   date, nullable         line_uid
    freight_amount    Decimal(18, 3)
    other_charges     Decimal(18, 3)
    evidence_reference  text — where the paper or PDF lives
    status        DRAFT → SUBMITTED → AWARDED | DECLINED | EXPIRED
```

**PRC-013.** A quotation has no stock and no accounting effect. It is evidence.

**PRC-014.** Comparison normalises to **base quantity and base unit price**
before comparing anything. Two suppliers quoting "one box of rice" are not
comparable until both boxes are expressed in kilograms — that is exactly the
mistake the item-specific conversion model exists to prevent, and a comparison
screen that showed raw package prices side by side would reintroduce it in the
one place where money is decided.

**PRC-015.** Freight and other charges are shown **separately and also
included** in a landed base unit price, both figures visible. Hiding freight
inside the unit price makes a cheap supplier with expensive delivery look
cheap; hiding it outside makes them look cheaper than they are.

**PRC-016.** **No automatic lowest-price award.** The system ranks and shows;
a human awards, names the quotation, and records a reason. Lowest price is not
the same decision as best value, and a system that awards silently removes the
one place where a buyer's judgement is recorded.

The award lives on the request:

```
PurchaseRequest.awarded_quotation  FK, nullable
                awarded_by / awarded_at / award_reason
```

**PRC-017.** Awarding is permitted only where the awarded quotation belongs to
the same organization and references the same request. The approver of the
award is not necessarily the requester, and `award_reason` is mandatory when
the awarded quotation is not the cheapest by landed base unit price.

---

## 6. Purchase orders and change control

```
PurchaseOrder                       PurchaseOrderLine
    organization / branch               order        FK CASCADE
    supplier      FK PROTECT             item         FK PROTECT
    number        per-organization       package_unit FK, nullable
    request / quotation  FK, nullable    ordered_quantity   entered + base
    warehouse     FK, destination        conversion / factor / version snapshot
    location      FK, nullable           unit_price   Decimal(18, 6)
    expected_date date                   line_total   Decimal(18, 3)
    payment_terms_days  snapshot         line_uid
    currency      IQD only in Release 1
    status        DRAFT → APPROVED → ISSUED
                  → CLOSED | CANCELLED
    version       PositiveInteger, starts at 1
```

**PRC-018.** A purchase order **creates no stock and no payable**, in any
status including `ISSUED`. It is a commitment, and a commitment is not a
liability under the accounting policy this system implements. Open orders are
reported (§12) and never posted.

**PRC-019.** Once `ISSUED`, the commercial terms — supplier, item, quantity,
price, package, terms — are **immutable on the row**. A change produces a new
**version**: the current row's terms are copied into `PurchaseOrderVersion`
with the revision reason, and the order's `version` increments. History is
readable forever; the current state is one row.

**PRC-020.** A revision may not reduce an ordered quantity **below what has
already been received** against that line. The receipt is a fact; the order is
a plan, and a plan cannot retroactively un-happen a delivery.

**PRC-021.** The supplier may not change once any receipt exists against the
order. Cancel and raise a new order.

**PRC-022.** Cancellation requires a reason, is refused once any receipt
exists, and is terminal.

**PRC-023.** Over-receipt policy is **conservative and configurable at zero**:
Release 1 refuses a receipt whose cumulative accepted quantity would exceed the
ordered base quantity on that line. A genuine over-delivery is handled by
revising the order upward first, which leaves a record of who agreed to buy
more. A tolerance percentage may be introduced later as an organization
setting; it is not introduced silently.

---

## 7. Goods receipt and inspection

```
GoodsReceipt                        GoodsReceiptLine
    organization / branch               receipt      FK CASCADE
    supplier      FK PROTECT             order_line   FK, nullable
    order         FK, nullable           item         FK PROTECT
    number        per-organization       package_unit FK, nullable
    received_at   business date          delivered_quantity  entered + base
    warehouse     FK                     accepted_quantity   entered + base
    location      FK, nullable           rejected_quantity   entered + base
    delivery_reference  the supplier's   measured_quantity   for VARIABLE
                        own note number  conversion / factor / version snapshot
    inspected_by  FK, nullable           unit_price   Decimal(18, 6) snapshot
    status        DRAFT → POSTED         lot / expiry_date
                  → REVERSED             rejection_reason  FK ReasonCode, nullable
                                         line_uid
```

**PRC-024.** `delivered = accepted + rejected`, enforced by a database
`CheckConstraint`. A line that does not add up is not a line.

**PRC-025.** **Only the accepted quantity increases stock.** Rejected goods
never entered inventory: they are recorded so the supplier can be argued with
and so quality can be reported on, and they post nothing. This is the single
most commonly broken rule in restaurant purchasing software and the reason the
inspection fields live on the receipt rather than on a later document.

**PRC-026.** A `VARIABLE` package line requires `measured_quantity`. Twelve
lambs is not a quantity of meat; the scale reading is. Inventory already
refuses this (`test_a_variable_package_requires_the_measured_quantity`) and
procurement must not offer a path around it.

**PRC-027.** A lot is required where the item tracks lots and prohibited where
it does not; an expiry date requires lot tracking. These are the item's rules
and procurement inherits them unchanged.

**PRC-028.** A receipt with **no purchase order is permitted** and is a normal
case for a small restaurant buying meat from the market. The line then carries
its own entered price. What a receipt may not do is exist without a price:
value with no number is how zero-cost stock gets created.

**PRC-029.** The receipt posts through **the existing inventory kernel** —
`apps.inventory` posting services, the same advisory locks, the same moving
average, the same period validation, the same immutability triggers.
Procurement adds no second posting path. If procurement needed a movement type
inventory does not have, that is a change to inventory, made in inventory, with
inventory's tests.

**PRC-030.** Partial receipt is normal: many receipts may reference one order,
and a line's cumulative accepted quantity is tracked against its order line.

**PRC-031.** A posted receipt is immutable and is corrected by **reversal plus
a replacement receipt** — never by editing. A reversal is refused if the
received stock has since been consumed below the reversal quantity, exactly as
inventory already refuses it (`test_a_receipt_reversal_respects_availability`).

---

## 8. GRNI: the accounting of a receipt

At the moment goods are accepted, the business owns stock and owes *something*
— but not a stated amount, because no invoice has arrived. That gap is what
`GOODS_RECEIVED_NOT_INVOICED` exists for. The account already exists
(`2-01-02-001`), the role already exists, and it is deliberately
organization-scoped: which item arrived says nothing about who is owed for it.

```
Dr  Inventory control      accepted_base_quantity × unit_price
    Cr  GRNI                                       the same figure
```

**PRC-032.** The receipt's journal value **equals** the stock value it posted,
to the last of three decimal places, for every line. This is inventory
invariant 21 extended one document: what enters the ledger enters the accounts.

**PRC-033.** `INVENTORY_CONTROL` is item-overridable and `GRNI` is not, so a
receipt of five items mapping to three control accounts produces **three debit
lines and one credit line**. The grouped-debit shape is already implemented and
tested for opening stock
(`test_grouped_debits_when_items_resolve_to_different_accounts`) and is reused
rather than reinvented.

**PRC-034.** No procurement posting service names an account, an account id, or
an account code. Every account comes from an effective-dated role mapping
resolved at the business date (ADR-019). A posting whose role has no mapping on
that date **fails and rolls back everything** — stock, journal and document
status together (PRC-036).

**PRC-035.** The receipt carries a complete source identity — organization,
document type, `public_id`, and a `SourceEvent` — or none of it. ADR-017 admits
no partial identity, and `SourceEvent` is extended with code and tests, never
with a free string.

**PRC-036.** Document, stock movement, journal entry and status change succeed
or fail as **one database transaction**. The plan states it as a hard rule and
the kernel already enforces it; procurement inherits it.

---

## 9. Supplier invoices, matching, and variance

```
SupplierInvoice                     SupplierInvoiceLine
    organization / branch               invoice      FK CASCADE
    supplier      FK PROTECT             item         FK PROTECT, nullable
    supplier_invoice_number             account       FK, nullable — for a
                  unique per supplier                 non-inventory charge
    invoice_date  date                   quantity     Decimal(18, 3), base
    due_date      derived from terms     unit_price   Decimal(18, 6)
                  snapshot, stored       line_total   Decimal(18, 3)
    order / receipt references           line_uid
    freight_amount / discount_amount
    total_amount  = SUM(lines)
    status        DRAFT → APPROVED → POSTED → REVERSED
```

**PRC-037.** `supplier_invoice_number` is unique per supplier — a partial
unique index over non-reversed invoices. Paying the same invoice twice is the
most expensive ordinary mistake in accounts payable, and the database is the
right place to make it impossible.

**PRC-038.** An invoice **never mutates stock**. Not a quantity, not a lot, not
a movement.

**PRC-039.** `total_amount` is the **sum of the posted lines**, never rounded
independently of them (`CLAUDE.md`, ADR-012). Freight and discount are
allocated across lines with `apps/core/allocation.py` — largest remainder, an
explicit unique `sequence` per line — never by rating each line and rounding.

### Three-way matching

```
MatchAllocation
    invoice_line   FK CASCADE
    receipt_line   FK PROTECT
    order_line     FK, nullable — derived from the receipt line where present
    matched_quantity  Decimal(18, 3), base units, > 0
    matched_value     Decimal(18, 3)
    created_by / created_at
```

**PRC-040.** Allocation is **many-to-many and partial**: one invoice line may
match several receipt lines; one receipt line may be matched by several
invoices. A month's deliveries invoiced once, and one delivery invoiced across
two documents, are both ordinary.

**PRC-041.** **Over-allocation is impossible.** The sum of `matched_quantity`
against a receipt line may not exceed its accepted base quantity, and the sum
against an invoice line may not exceed its quantity. Enforced in the service
under a row lock, and verified by a reconciliation invariant — the same
belt-and-braces the ledger uses, because a check that only exists inside one
service is one refactor away from not existing.

**PRC-042.** Matching status per invoice line is **derived, never stored as a
mutable flag**: `UNMATCHED`, `PARTIALLY_MATCHED`, `MATCHED`, `EXCEPTION`.

### The posting, and where variance goes

For the matched portion, the invoice clears what the receipt parked:

```
Dr  GRNI                       matched receipt value  (what the receipt said)
Dr  Purchase price variance    the difference         (when the invoice is dearer)
    Cr  Supplier payable       invoiced value         (what is actually owed)
```

with the variance line reversed in sign when the invoice is cheaper, and absent
entirely when the two agree — which is the common case and must produce a clean
two-line entry, not a zero-value third line (the kernel refuses those).

**PRC-043.** **Price variance does not restate a posted movement.** ADR-018's
moving average is a function of posting order; retroactively repricing a
receipt would restate every issue that followed it, including issues in closed
periods. The variance is recognised where it is discovered.

**PRC-044.** Where the received stock is **still on hand**, the organization
may elect to carry the variance into inventory value as an explicit
**revaluation** — a value-only adjustment with no quantity, which the inventory
kernel already supports and tests
(`test_a_value_only_write_up_moves_no_quantity`). Where the stock is wholly or
partly consumed, the consumed proportion goes to the variance account. The
split is deterministic: proportion on hand at the invoice's business date,
allocated with `apps/core/allocation.py`.

**PRC-045.** Release 1 default is **variance to the expense account, no
automatic revaluation**, because automatic revaluation of partly-consumed stock
produces a figure nobody can explain from a document. Revaluation is an
explicit act with its own permission. This is a policy decision and is recorded
in ADR-022 rather than buried in a service.

**PRC-046.** Landed cost — freight capitalised into inventory value rather than
expensed — is **specified but not implemented in Release 1**, and §16 says why.
`freight_amount` is captured on the quotation, order and invoice so the data
exists when the policy is approved.

---

## 10. Supplier returns and credit notes

These are two events (PRC-001) and either can occur without the other.

```
SupplierReturn                      SupplierCreditNote
    organization / branch               organization / branch
    supplier / receipt  FK               supplier         FK PROTECT
    number / returned_at                 supplier_document_number
    warehouse / location                 credit_date
    reason        FK ReasonCode          invoice / return references
    status  DRAFT → POSTED → REVERSED    amount / reason
                                         status  DRAFT → POSTED → REVERSED

SupplierReturnLine  (amended in, Task 2.13)
    supplier_return         FK CASCADE, sequence unique per return
    goods_receipt_line      FK PROTECT — the delivery line coming back
    item / lot              copied from that line
    returned_base_quantity  3 dp, > 0, bounded per delivery line
    posted_value            what the kernel removed, written by posting only
    expected_credit_value   the claim, metadata, posts nothing
    movement / accounts     stamped by posting, frozen by trigger
```

**Amendment (Task 2.13): the header sketch alone cannot post.** It records
*that* something was returned and never *what* — not which item, not how much,
not from which lot — so there is no quantity to move, no value to post, and no
way to stop the same fifty kilograms being returned twice. The line model
above was added, one line per source `GoodsReceiptLine`, with the returnable
bound derived per delivery line: accepted quantity less what standing returns
have already taken (REVERSED ones release their claim). A wholly rejected
line never entered stock and cannot be returned from it.

**PRC-047.** A supplier return is **not** an inventory `RETURN_IN`. `RETURN_IN`
means goods coming back from a kitchen to a store, at the cost they were issued
at. A supplier return sends goods *out* of the business. They are opposite
directions and must not share a movement type.

**PRC-048.** A return leaves stock at the **standing moving average**, like
every other outbound, and a full depletion surrenders the entire remaining book
value (ADR-018). This is not negotiable without a per-layer cost model the
system does not have, and inventing one for returns alone would leave two
costing methods in one warehouse.

**PRC-049.** The difference between what left inventory (average cost) and what
the supplier agrees to credit (usually the original price) is a **purchase
return variance**, posted to the variance account. Recorded in ADR-022 with the
worked example, because the first person to see a return that credits more than
it removed will otherwise assume a bug.

> **Amendment (Task 2.13, ADR-022 §2 as amended):** the variance is recognised
> at the **credit note**, not at the physical return — at the gate nobody knows
> what the supplier will credit, and a figure nobody has agreed must not reach
> the profit and loss. The return posts `Dr SUPPLIER_RETURN_CLEARING /
> Cr INVENTORY_CONTROL` for the book value that left, and nothing else; the
> clearing balance is the claim outstanding, and Task 2.14 clears it against
> the agreed credit with the difference going to `PURCHASE_RETURN_VARIANCE`.

**PRC-050.** Negative stock is refused on a return, by the kernel, with no
procurement-specific bypass.

**PRC-051.** A credit note reduces the supplier payable, or stands as an
unallocated supplier credit if it references no invoice. It never moves stock.

**PRC-052.** `supplier_document_number` is unique per supplier over
non-reversed credit notes — the same duplicate protection invoices get, and for
the same reason in reverse.

---

## 11. Supplier payments and allocations

```
SupplierPayment                     PaymentAllocation
    organization / branch               payment    FK CASCADE
    supplier      FK PROTECT             invoice    FK PROTECT
    number / paid_at                     amount     Decimal(18, 3), > 0
    method        CASH | BANK            created_by / created_at
    amount        Decimal(18, 3)
    reference     cheque or transfer id
    status  DRAFT → POSTED → REVERSED
```

```
Dr  Supplier payable    allocated amount
Dr  Supplier advance    unallocated remainder, where any
    Cr  Cash or bank    the full amount
```

**PRC-053.** Partial payment is normal and a payment may allocate across
several invoices.

**PRC-054.** **Over-allocation is impossible**: the sum of allocations against
one invoice may not exceed its total, and the sum of allocations on one payment
may not exceed its amount. Both checked under a lock and both re-checked by
reconciliation.

**PRC-055.** An unallocated remainder is a **supplier advance** (an asset), not
a negative payable. Netting a prepayment against a payable that does not exist
yet makes the aging report lie about both.

**PRC-056.** The cash or bank account comes from an **effective-dated role**
resolved by `method`, never a hard-coded id (ADR-019). Two new roles:
`SUPPLIER_PAYMENT_CASH` and `SUPPLIER_PAYMENT_BANK`. When Phase 5 introduces
cashboxes and bank accounts as first-class objects, the payment names one and
the role becomes its default — the model widens, nothing already posted moves.

**PRC-057.** Allocation to the oldest invoice is offered as a **default the
user can see and change**, never applied silently.

---

## 12. Reports

Every report obeys the Phase 1 contract: a named cutoff mode (inventory
invariant 35), organization and branch scope from memberships, cost columns
omitted rather than blanked without `view_valuation`, exact Decimals in CSV
with formula neutralisation, HTMX filters that survive pagination, and **no
repair button** anywhere.

| Report | Answers |
|---|---|
| Supplier aging | What is owed, in buckets, by document date and due date |
| Supplier statement | Every document and allocation for one supplier, running balance |
| Open purchase orders | Ordered, received, outstanding, by supplier and item |
| Outstanding receipt quantity | Ordered but not delivered |
| GRNI exceptions | Received and not invoiced, ageing — the account that must reconcile |
| Invoice without receipt | Invoiced and never delivered |
| Matching exceptions | Quantity and price variances beyond tolerance |
| Purchase spend | By supplier, item, category, period |
| Price variance | Where the invoice differed from the order, and by how much |
| Return and credit status | Returned, credited, outstanding |
| Payment allocations | What a payment covered, and what remains unallocated |
| Procurement-to-GL | Supplier subledger vs payable account; GRNI vs unmatched receipts |

**PRC-058.** The Procurement-to-GL reconciliation is the phase's proof
obligation and mirrors `verify_inventory_accounting`:

1. Sum of open supplier balances **equals** the supplier payable account
   balance.
2. Sum of accepted-and-unmatched receipt value **equals** the GRNI account
   balance.
3. Every posted procurement journal traces to exactly one source document.
4. No document is posted twice; no allocation exceeds its parent.

**PRC-059.** Verification **reports and refuses to repair**, like
`verify_stock_projection`. A repair button on a reconciliation screen is a
button that hides the bug it was built to find.

---

## 13. Permissions and scope

Every permission is a permission **plus a scope** (ADR-016). Out of scope is
404; in scope without authority is 403.

| Permission | Scope | Notes |
|---|---|---|
| `view_supplier`, `manage_supplier` | organization | Master data |
| `view_supplier_item`, `manage_supplier_item` | organization | Catalogue |
| `view_purchase_request`, `create_purchase_request` | branch | |
| `approve_purchase_request` | branch | Never the submitter |
| `view_quotation`, `manage_quotation` | organization | |
| `award_quotation` | organization | Records a reason |
| `view_purchase_order`, `create_purchase_order` | branch | |
| `approve_purchase_order`, `issue_purchase_order` | branch | |
| `revise_purchase_order`, `cancel_purchase_order` | branch | |
| `view_goods_receipt`, `post_goods_receipt` | **warehouse** | It moves stock |
| `inspect_goods_receipt` | warehouse | Accept/reject decision |
| `reverse_goods_receipt` | warehouse | Elevated |
| `view_supplier_invoice`, `create_supplier_invoice` | organization | |
| `approve_supplier_invoice`, `post_supplier_invoice` | organization | |
| `match_supplier_invoice` | organization | |
| `view_supplier_return`, `create_supplier_return` | **warehouse** | Amended in, Task 2.13 |
| `post_supplier_return` | warehouse | It moves stock |
| `reverse_supplier_return` | warehouse | Elevated, like the receipt's |
| `post_supplier_credit_note` | organization | |
| `post_supplier_payment` | organization | Money leaves |
| `view_supplier_cost` | organization | Prices, separate from quantity |
| `view_procurement_report` | organization | |
| `import_supplier`, `import_supplier_item` | organization | |

**PRC-060.** Receipt and return permissions are **warehouse-scoped**, because
they move stock and inventory already scopes stock movement that way. Invoice
and payment permissions are organization-scoped, because money is not stored in
a warehouse.

> **Amendment (Task 2.13):** the original table named `post_supplier_return`
> alone, which would make the return the one posting document with no view, no
> create and no reversal permission. The three companions above were added on
> the receipt's exact pattern, and the separation of duties follows it too:
> the storekeeper records and posts a return (sending goods back is warehouse
> work), and **reversal is withheld** — undoing a posted movement reverses a
> journal as well as stock, and belongs to whoever answers for the figures.

**PRC-061.** Cost visibility is separate from document visibility, exactly as
in inventory: a storekeeper receiving goods sees quantities and lots, and the
price column is **omitted, not blanked** (`test_a_storekeeper_sees_quantity_
and_no_cost_at_all` is the pattern).

**PRC-062.** No generic writable CRUD API and no writable admin for any posted
procurement record. Command endpoints only; admin read-only. This is inventory
invariant 16, and the import boundary test that proves it extends to
procurement models.

---

## 14. API, UI and demo

**PRC-063.** The API is commands, not CRUD: `submit_purchase_request`,
`approve_purchase_order`, `post_goods_receipt`, `match_invoice_line`,
`allocate_payment`. Every money and quantity value crosses the wire as an
**exact string** in both directions — JSON numbers are binary floats before any
Python code sees them.

**PRC-064.** Every command carries an organization-scoped idempotency key
matched against a request fingerprint. A key reused with a different payload is
`idempotency_key_conflict`, not a retry (ADR-017).

**PRC-065.** Screens are Arabic-first, RTL, inside the existing shell, using
CSS logical properties only, with HTMX for list filtering, pagination and
dependent selects (supplier → catalogue item → package). Filters survive
pagination through `apps.core.context_processors._filter_query`, which already
serves three list families.

**PRC-066.** Demo data: exactly **three suppliers** —
`DEMO-MEAT-SUPPLIER` / مورد اللحوم — تجريبي,
`DEMO-CHICKEN-SUPPLIER` / مورد الدجاج — تجريبي,
`DEMO-GROCERY-SUPPLIER` / مورد المواد الغذائية — تجريبي —
against the **five existing inventory demo items**. No new items, no dozens of
suppliers. It extends the existing demo tooling, posts through real services,
is `DEBUG`-only, is idempotent, and leaves every procurement screen showing
something. `docs/development/demo-data-policy.md` governs it.

---

## 15. Source identity and new `SourceEvent` codes

| Document | `SourceEvent` |
|---|---|
| Goods receipt | `PROCUREMENT_GOODS_RECEIPT` |
| Goods receipt reversal | `PROCUREMENT_GOODS_RECEIPT_REVERSAL` |
| Supplier invoice | `PROCUREMENT_SUPPLIER_INVOICE` |
| Supplier invoice reversal | `PROCUREMENT_SUPPLIER_INVOICE_REVERSAL` |
| Supplier return | `PROCUREMENT_SUPPLIER_RETURN` |
| Supplier return reversal | `PROCUREMENT_SUPPLIER_RETURN_REVERSAL` |
| Supplier credit note | `PROCUREMENT_SUPPLIER_CREDIT_NOTE` |
| Supplier credit note reversal | `PROCUREMENT_SUPPLIER_CREDIT_NOTE_REVERSAL` |
| Supplier payment | `PROCUREMENT_SUPPLIER_PAYMENT` |
| Supplier payment reversal | `PROCUREMENT_SUPPLIER_PAYMENT_REVERSAL` |

**PRC-067.** `source_document_id` is the document's immutable `public_id`, never
its primary key and never its human-readable number. A number can be corrected;
a UUID cannot, and the journal has to still point at something in five years.

### New account roles

| Role | Scope | Account |
|---|---|---|
| `SUPPLIER_PAYABLE` | organization | `2-01-01-001` |
| `PURCHASE_PRICE_VARIANCE` | organization | new: `5-02-01-001` |
| `SUPPLIER_ADVANCE` | organization | new: `1-04-01-001` |
| `SUPPLIER_PAYMENT_CASH` | organization | `1-01-01-001` |
| `SUPPLIER_PAYMENT_BANK` | organization | `1-01-02-001` |

`GOODS_RECEIVED_NOT_INVOICED` and `INVENTORY_CONTROL` already exist and are
reused unchanged.

> **Amendment (Task 2.12):** `PURCHASE_PRICE_VARIANCE`'s `5-02-01-001` is
> superseded by `8-01-03-001`, class CLEARING — see the header note and
> ADR-022 §5.
>
> **Amendment (Task 2.13):** this table omitted the return's accounts
> entirely. Two roles were added, both organization-scoped, both recorded in
> ADR-022 §5 as amended: `SUPPLIER_RETURN_CLEARING` → `8-01-04-001` (class
> CLEARING, mapped and posted to by the return) and `PURCHASE_RETURN_VARIANCE`
> → `7-09-04-001` (class OTHER — a bidirectional difference is never cost of
> sales — seeded as vocabulary and deliberately unmapped until Task 2.14
> posts to it).

---

## 16. What this specification deliberately does not do

Naming these is the point. An omission that is written down is a decision; one
that is not is a defect waiting to be discovered by an accountant.

1. **No tax of any kind.** No VAT, no withholding, no reverse charge. The
   source documents define none, and inventing a tax treatment would produce
   numbers that look authoritative and are not. When the requirement arrives it
   is a task with an ADR, and the line-level structure above accommodates it.
2. **No landed-cost capitalisation** (PRC-046). Freight is captured but
   expensed. Allocating freight into inventory value requires an approved
   allocation basis — by value, by weight, by line count — and the wrong choice
   silently misprices every recipe downstream.
3. **No multi-currency.** IQD only. The field exists; conversion does not.
4. **No supplier portal, no RFQ email, no EDI.**
5. **No automatic reordering.** Reorder points already report; converting them
   into orders without a human is a separate decision.
6. **No blanket or standing orders**, no scheduled deliveries.
7. **No consignment stock.** Goods owned by the supplier while sitting in our
   warehouse would break the assumption that stock on hand is stock owned.
8. **No direct import of a posted document.** Supplier and catalogue master
   data import; drafts import for review. A posted receipt, invoice, return or
   payment is only ever created by its service, because an import that posts is
   a posting path with no permission check on it.
9. **No purchase-price approval workflow beyond the award and the order
   approval** already specified.

---

## 17. Task breakdown

Dependency order. Each is a separate commit with its own tests, gates and demo
data; none begins before its predecessor is green.

| Task | Delivers | Depends on |
|---|---|---|
| 2.1 | Supplier master, permissions, API, Arabic screens, demo | — |
| 2.2 | Supplier item catalogue, effective dating, versioning | 2.1 |
| 2.3 | Purchase requests, maker-checker, no ledger effect | 2.2 |
| 2.4 | Supplier quotations | 2.3 |
| 2.5 | Quotation comparison and award | 2.4 |
| 2.6 | Purchase orders | 2.5 |
| 2.7 | PO revision, versioning and cancellation | 2.6 |
| 2.8 | Goods receipt and inspection, inventory posting | 2.6 |
| 2.9 | GRNI accounting, reconciliation, drill-down | 2.8 |
| 2.10 | Supplier invoices and the payable | 2.9 |
| 2.11 | Three-way matching and allocations | 2.10 |
| 2.12 | Price and quantity variance accounting | 2.11 |
| 2.13 | Supplier returns | 2.9 |
| 2.14 | Supplier credit notes | 2.13 |
| 2.15 | Supplier payments and allocations | 2.10 |
| 2.16 | Procurement reports and Procurement-to-GL reconciliation | 2.15 |
| 2.17 | Imports, demo completion, security hardening | 2.16 |
| 2.18 | Phase 2 exit gate | all |

## 18. Proposed ADRs

Only two, and only because each records a policy that outlives its
implementation and that a future reader would otherwise reconstruct wrongly.

- **ADR-022 — Supplier return valuation and purchase variance treatment.**
  Why a return leaves at the standing average and not at the receipt price;
  why price variance does not restate posted movements; when revaluation is
  permitted and who may do it. (PRC-043 – PRC-045, PRC-048, PRC-049.)
- **ADR-023 — GRNI clearing and three-way matching allocations.** What
  "matched" means, why allocations are rows rather than a status field, why
  over-allocation is a database-verified invariant, and how a partly matched
  invoice reports. (PRC-040 – PRC-042, PRC-058.)

No ADR is proposed for supplier ownership (ADR-007 already settles it), for
account resolution (ADR-019), or for source identity (ADR-017). Restating a
decision in a second document is how two documents come to disagree.
