# ADR-023 — GRNI clearing and three-way matching allocations

- **Status:** **Accepted** (Task 2.12, 2026-08-14), amended in three places where
  the shipped implementation settled a detail differently. Not accepted at Task
  2.11 as originally planned, because §1's GRNI equality could not be true until
  something cleared the account, and that is Task 2.12.
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

**Amendment (Task 2.12), two corrections to that sentence.**

*"Matched" means "cleared".* Task 2.11 gave matching a draft stage, and a draft
or ready match consumes availability while clearing nothing: the evidence is
agreed but nobody has been billed. GRNI is released when an invoice **posts**,
so the equality's right-hand side counts allocations whose match carries a live
posting. Availability and clearing are two sets with two purposes, and
conflating them is how a matching workspace comes to disagree with its ledger.

*The account is shared.* Task 1.4's uninvoiced stock receipts credit the same
GRNI account, and they are not procurement's to explain. The implemented
equality is therefore over procurement's **own** contribution: what its
documents put into GRNI, less what its invoices took out, equals the value of
its accepted delivery lines that no posted invoice covers.

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

Locking is on the **parent lines**, acquired in a canonical order, so two
people matching the same delivery from two screens serialise rather than
deadlock. This is the `_StockKey.sort_key` discipline from Task 1.2 applied to
a different pair of tables.

**Amendment (Task 2.12).** The implemented order is match header, then invoice,
then invoice line, then receipt line, then order line, then the allocation rows
— not "receipt line then invoice line" as first written. Taking the invoice
header first is what serialises same-invoice contention before any line lock is
reached, and Task 2.12's posting extends the same order by taking the match
header **above** the invoice, even though its command names the invoice.

### 4. Quantity variance and price variance are different questions

- **Quantity variance** is ordered versus accepted. It is a fact about the
  delivery and it is settled at the receipt: only the accepted quantity ever
  entered stock, and the shortfall is reported, never posted.
- **Price variance** is receipt price versus invoice price for the *matched*
  quantity. It is settled at the invoice, and ADR-022 §3 governs where it goes.

Conflating them produces the classic mess where a short delivery looks like a
pricing dispute. They are reported separately for the same reason.

### 5. An invoice may be posted with no matching receipt, and it is an exception, not an error

**Amendment (Task 2.12): deferred, not implemented.** A goods line with no
match still refuses to post, with `invoice_awaiting_matching`. The reason is
that this section's own prescription — *"the debit goes to the item's or line's
account"* — resolves to `INVENTORY_CONTROL` for a goods line, and debiting
inventory value with no stock movement behind it breaks the inventory-to-GL
equality `verify_inventory_against_gl` checks, by the whole invoice amount.
Task 2.0 §16 defines no `UNRECEIVED_INVENTORY_CLEARING` account and Task 2.12
did not invent one. The dispute this section wants visible is real; recording
it needs an account somebody has approved.

> The paragraph that stood here argued the opposite of the amendment above —
> "the invoice posts, the GRNI line is absent, the debit goes to the item's
> or line's account" — and was left in place when the amendment was written.
> The section therefore asserted both that the invoice posts and that it
> refuses to, and the shipped system refuses. It is deleted rather than kept
> as history because a future task reading the stale half would build a path
> that breaks the inventory-to-GL equality by the whole invoice amount, which
> is precisely what the amendment exists to prevent. The argument it made is
> not lost: the dispute is real and still needs recording, which is why the
> amendment says the account has to be approved rather than invented, and why
> the **invoice-without-receipt report** exists today and lists exactly these
> lines. (Found at the Phase 2 gate, in the same sweep that found the same
> superseded claim in `docs/decisions/README.md` and
> `docs/invariants/procurement-invariants.md`.)

The reverse — received and never invoiced — is the GRNI ageing report, and a
GRNI line growing old is the single most useful signal procurement produces
about a supplier's paperwork.

### 6. Every allocation is attributable

`created_by` and `created_at` on every row, plus an audit event. Matching is a
judgement — deciding that *this* invoice line covers *that* delivery — and a
judgement with no name attached is not auditable.

**Amendment (Task 2.12).** The original sentence read *"Allocations are deleted
only by reversing the invoice, never edited"*, and neither half survived
contact with the implementation. Allocations are added and removed only while
their match is `DRAFT`; once `READY` a trigger freezes them, and reversing the
invoice **cancels** the match rather than deleting its rows — the withdrawn
answer is history worth keeping, and its reason is on it. Nothing on any path
deletes an allocation once its match has left draft.

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
