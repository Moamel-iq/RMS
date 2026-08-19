# ADR-030 — Cash, bank, expense vouchers, accruals and prepayments

- **Status**: Accepted
- **Date**: 2026-08-19
- **Task**: 5.0 — Accounting domain specification, and Phase 5 checkpoints 3, 5, 6
- **Related**: ADR-012 (monetary precision and allocation), ADR-014 (chart of
  accounts), ADR-019 (account roles), ADR-023 (GRNI clearing)
- **Companions**: ADR-029 (accounting operations and manual journals), ADR-031
  (financial statements and year-end close)

---

## 1. A cashbox is master data with one account, and no balance

**Decision.** `Cashbox` and `BankAccount` are master records. Each names
exactly one postable GL account, and **neither carries a balance field of any
kind**.

The balance on a cashbox page is `account_balance(account=…, branch=…,
up_to=…)` over posted lines, computed when the page is requested. The
statement is the same lines, ordered and accumulated.

**Why not a stored balance.** Because a stored balance has to be maintained,
and every maintenance path is a chance to disagree with the ledger. The
disagreement is silent: the cashbox page says 4,100,000 and the trial balance
says 4,090,000, and nothing in the system is required to notice. Deriving it
costs an aggregate query and cannot be wrong.

**One account, one active cashbox.** A partial unique constraint on
`(organization, account)` where `is_active`. Two active cashboxes sharing a GL
account produce two statements that are each individually plausible and are
both the *same* movements — an operator counting one drawer against it would
find it over by exactly the other drawer.

The constraint is partial rather than total so an archived cashbox can be
replaced without renumbering: the account is free again once the old row is
archived, and the archived row keeps its history readable forever.

**No hard delete.** Neither model has a delete route, a delete API, or a
cascade that could remove one. Documents reference them with `PROTECT`.

## 2. Release 1 does not import bank statements

**Decision.** No bank-statement file import ships in Release 1, and **no screen
advertises one**. A disabled button labelled "استيراد كشف الحساب" is a promise
the system does not keep, and the operator who plans a month's reconciliation
around it discovers that at the worst possible moment.

The bank page shows unreconciled items — movements the accountant has not yet
ticked against the bank's own statement — because that is useful with or
without an import. The import itself is a later task with its own format
question, which no existing generic importer answers completely.

---

## 3. An expense voucher is for what Procurement is not for

**Decision.** `ExpenseVoucher` records a **non-supplier operational expense
paid immediately**: the electricity bill, a taxi, a municipal fee, a repair
paid in cash.

It is explicitly not for supplier invoices, inventory purchases, delivery-app
settlements, payroll, production posting or sales discounts. Each of those has
a document that posts its own journal under its own controls. A second path to
the same journal is a second set of rules over one economic event, and the
weaker path wins because it is the one an operator finds first.

Two model decisions enforce that boundary rather than merely stating it:

**No supplier foreign key.** The moment an expense voucher can name a supplier
it becomes a supplier invoice with no three-way match, no GRNI clearing, no
purchase-price variance and no credit-note path — and it will be used as one,
because it is faster.

**No tax field.** Release 1 has no approved Iraqi tax policy, and a field
labelled "ضريبة" would invite one to be invented per voucher by whoever fills
it in.

**Lifecycle** `DRAFT → APPROVED → POSTED → REVERSED`, with the creator barred
from approving. The posting is `Dr expense/asset lines · Cr the pay-from
account's GL account` — a cashbox or a bank account, exactly one, resolved
through the master record rather than by naming a GL account directly, so the
voucher's cash effect appears on the cashbox statement automatically.

**An unpaid expense is not an expense voucher.** It is an accrual. The
temptation is to let a voucher post to a generic payable and settle later, and
that generic payable is a supplier subledger with no supplier — an unaged,
unallocatable liability nobody can reconcile. §4 is the supported path.

---

## 4. An accrual recognises an expense once, and says how it stops

**Decision.** `AccrualDocument` posts `Dr Expense · Cr
ACCRUED_EXPENSES_PAYABLE` for a cost incurred but not yet invoiced.

The hard part is not the posting. It is what happens six weeks later when the
real invoice arrives, because the obvious behaviours are both wrong: posting
the invoice on top of the accrual recognises the expense twice, and letting the
accrual quietly linger overstates the liability forever.

So the accrual carries an **optional link to the `SupplierInvoice` that
replaces it**, and linking is not creating. Accounting never creates a supplier
invoice — that document belongs to Procurement and arrives through Procurement.
When it does, `clear_accrual` reverses the accrual journal explicitly, with an
actor and a reason, and the expense stands recognised exactly once from the
invoice.

An optional **automatic reversal date in the next open period** covers the
common month-end case, where the accrual exists only to land the cost in the
right month and is meant to unwind on the first of the next.

---

## 5. A prepayment schedule sums to its total, exactly

**Decision.** `Prepayment` posts `Dr PREPAID_EXPENSE · Cr cash/bank` when paid,
and each `PrepaymentScheduleLine` posts `Dr Expense · Cr PREPAID_EXPENSE` as it
is consumed.

**The schedule is split with `apps/core/allocation.py`** — the certified
largest-remainder allocator, remainder DESC then `sequence` ASC — and never by
dividing the total by the period count and rounding each period.

This is the ADR-006 counterexample in a different costume. 1,000,000 over three
months at three decimal places is 333,333.333 per month, which sums to
999,999.999. The residual is one thousandth of a dinar and it is fatal: the
prepaid account never reaches zero, the balance sheet carries a permanent
0.001 asset, and the account cannot be closed at year end without a plug. The
allocator puts the remainder on a deterministic line and `Σ lines == total`
holds exactly.

**A posted schedule line is never rewritten.** Amending a prepayment re-plans
its `PLANNED` lines only. The alternative — recomputing the whole schedule —
would silently disagree with journals already in the ledger.

**Nothing posts into a closed period silently.** Amortization into a closed
month refuses and says which period and why. The accountant reopens it, or
posts the catch-up in the current month deliberately. What must not happen is
the system choosing one of those on its own.

---

## 6. Consequences

Four new document types, four new account roles (`ACCRUED_EXPENSES_PAYABLE`,
`PREPAID_EXPENSE`, and the two in ADR-031), and one new role domain
`ACCOUNTING` — the first whose posting rules are about the organization's own
financial administration rather than a trading module's.

Every one of them resolves its accounts through `resolve_default_account` and
the role indirection. **No account id or account code is hard-coded** in any
Phase 5 service, which is what lets a second organization run a different chart
without a code change.
