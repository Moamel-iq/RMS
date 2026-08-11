# ADR-023 — GRNI clearing and three-way matching allocations

- **Status:** **Proposed** (2026-08-11, Task 2.0). To be accepted at Task 2.11.
- **Date:** 2026-08-11
- **Related:** ADR-012 (money and allocation), ADR-017 (source identity and
  idempotency), ADR-018 (stock ledger), ADR-019 (account roles), ADR-022
  (variance treatment)
- **Detail:** `docs/tasks/task-2-0-procurement-domain-spec.md` §8, §9

Between the moment goods are accepted and the moment an invoice arrives, the
business owns stock and owes something it cannot yet name. This decision
settles what sits in that gap, and what it means to say an invoice has been
matched.

## Context

The receipt and the invoice are separate events on separate dates (Task 2.0
§1). That separation is the right model, and it creates an obligation: the
period between them has to be represented by something, and that something has
to reconcile.

The naive alternatives both fail. Posting the payable at receipt states a debt
to a supplier who has not yet said what it is. Posting nothing at receipt
leaves stock on the books with no corresponding liability, so the balance sheet
does not balance until the invoice arrives.

`GOODS_RECEIVED_NOT_INVOICED` is the answer, and the account and its role
already exist from Task 1.4 (`2-01-02-001`, organization-scoped).

## Decision

### 1. GRNI is a clearing liability, and its balance has a physical meaning

```
Goods receipt:      Dr Inventory control   Cr GRNI
Supplier invoice:   Dr GRNI (+ variance)   Cr Supplier payable
```

The GRNI balance at any moment **equals the value of accepted receipt lines
that no invoice has matched**. That is not a description; it is a testable
equality and it is invariant 47.

This is why GRNI is organization-scoped and not item-overridable. Which item
arrived says nothing about who is owed for it, and splitting the clearing
account per item would produce a set of balances that individually mean nothing
and collectively mean what one account already meant.

### 2. Matching is rows, not a status field

```
MatchAllocation
    invoice_line      FK CASCADE
    receipt_line      FK PROTECT
    order_line        FK, nullable
    matched_quantity  Decimal(18, 3), base units, strictly positive
    matched_value     Decimal(18, 3)
    created_by / created_at
```

**Why not a status column on the invoice line.** Because matching is genuinely
many-to-many and genuinely partial. A month of daily meat deliveries invoiced
once is many receipts to one invoice; a single delivery invoiced across two
documents is one receipt to many invoices; and an invoice covering nine of the
ten kilograms delivered is neither matched nor unmatched. A status field can
express none of those, so a system built on one starts inventing side tables
within weeks — and the side tables are these rows, arrived at by a worse route.

**Consequence:** matching status is **derived** — `UNMATCHED`,
`PARTIALLY_MATCHED`, `MATCHED`, `EXCEPTION` — computed from the allocations
every time it is asked for. No stored flag exists to drift out of agreement
with the rows beneath it. This is the same reasoning that keeps supplier
balances derived (Task 2.0 §2).

### 3. Over-allocation is impossible, and is checked twice

The sum of `matched_quantity` against a receipt line may not exceed its
accepted base quantity. The sum against an invoice line may not exceed its
quantity. Both are enforced **in the service, under a row lock on the parent
line**, and both are **re-verified by `verify_procurement_accounting`**.

The double check is deliberate and follows the pattern the stock ledger already
uses. A guard that lives only inside one service function is one refactor, one
new code path, or one management command away from not existing. A verifier
that recomputes the sums from the rows will notice.

Locking is on the **parent lines**, acquired in canonical order — receipt line
then invoice line, each by primary key ascending — so two people matching the
same delivery from two screens serialise rather than deadlock. This is the
`_StockKey.sort_key` discipline from Task 1.2 applied to a different pair of
tables.

### 4. Quantity variance and price variance are different questions

- **Quantity variance** is ordered versus accepted. It is a fact about the
  delivery and it is settled at the receipt: only the accepted quantity ever
  entered stock, and the shortfall is reported, never posted.
- **Price variance** is receipt price versus invoice price for the *matched*
  quantity. It is settled at the invoice, and ADR-022 §3 governs where it goes.

Conflating them produces the classic mess where a short delivery looks like a
pricing dispute. They are reported separately for the same reason.

### 5. An invoice may be posted with no matching receipt, and it is an exception, not an error

Refusing it would be worse. A supplier who invoices for goods never delivered
has created a real dispute, and a system that refuses to record the invoice
leaves the dispute invisible. The invoice posts, the GRNI line is absent, the
debit goes to the item's or line's account, and the `invoice-without-receipt`
report is where somebody deals with it.

The reverse — received and never invoiced — is the GRNI ageing report, and a
GRNI line growing old is the single most useful signal procurement produces
about a supplier's paperwork.

### 6. Every allocation is attributable

`created_by` and `created_at` on every row, plus an audit event. Matching is a
judgement — deciding that *this* invoice line covers *that* delivery — and a
judgement with no name attached is not auditable. Allocations are deleted only
by reversing the invoice, never edited.

## Consequences

- GRNI has a meaning that can be verified against documents, and a reconciler
  who disagrees with the balance can be shown the exact unmatched lines.
- Partial and many-to-many matching work from day one rather than being
  retrofitted.
- Matching status cannot drift, because it is not stored.
- Two people matching the same delivery concurrently serialise on the parent
  lines; neither can over-allocate.
- The verifier reports and **refuses to repair** (Task 2.0 §12). A repair
  button on a matching reconciliation would silently write allocations nobody
  decided.

## Alternatives rejected

**Post the payable at receipt.** States a debt whose amount nobody has claimed
yet, and leaves nothing to reconcile when the invoice differs.

**Post nothing at receipt.** Stock on the books with no liability behind it.

**A `matched` boolean on the invoice line.** Cannot express partial, cannot
express many-to-many, and drifts from the truth the moment either appears.

**Automatic matching by supplier and date.** Rejected for the same reason
quotations are not awarded automatically (Task 2.0 §5): it replaces a recorded
human judgement with a rule that is right most of the time, and the times it is
wrong are exactly the ones worth catching. Suggestion is fine; silent
allocation is not.
