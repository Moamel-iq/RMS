# Accounting module invariants (Phase 5)

The kernel's own invariants live in
`docs/specs/accounting-kernel-invariants.md` and are unchanged. These are the
**module** invariants Phase 5 adds on top of them: what the screens, the four
new document types and the reports must never do.

Each is stated as a property that can fail, with what enforces it. Where the
enforcement is a database constraint or trigger it is named; where it is a
service check, the service is named. "Reviewed" is not an enforcement and does
not appear.

---

## A. Derivation

**A1 — no mutable balance exists anywhere in accounting.**
No model added by Phase 5 carries a stored balance, running total, or
outstanding amount. Cashbox balance, bank balance, supplier outstanding,
application outstanding, account balance and every statement figure are
computed from `posted_lines(...)` at read time.
*Enforced by:* absence — plus `verify_accounting` check `no_stored_balance`,
which fails on any accounting field whose name matches
`balance|outstanding|total_due` outside a document line.

**A2 — every balance counts POSTED and REVERSED entries and no others.**
A reversal is itself posted and its original stays in the ledger, so both
belong. Drafts never do.
*Enforced by:* `selectors.posted_lines`, which every report calls.

**A3 — a document total is the sum of its posted lines.**
Never rounded independently. Applies to expense vouchers, accruals and
prepayment schedules exactly as it applies to journals.
*Enforced by:* service-side recomputation on every mutation; `verify_accounting`
check `document_totals_are_line_sums`.

---

## B. The ledger stays the only ledger

**B1 — Accounting creates no second subledger.**
No `SupplierBalance`, no application balance model, no cash ledger, no stock
value table. `ذمم الموردين` and `ذمم التطبيقات` are read workspaces over
Procurement and Sales.
*Enforced by:* review of the model list in `verify_accounting` check
`no_duplicate_subledger`, which enumerates accounting models and fails on any
whose name matches the forbidden set.

**B2 — Accounting never writes a source-domain record.**
No Accounting view, service or command calls `save()` on a Procurement, Sales,
Inventory or Kitchen model.
*Enforced by:* `apps/accounting` imports the source apps' **selectors** only;
test `test_accounting_writes_no_source_record` greps the module for source-model
mutation.

**B3 — dependency direction is one-way.**
Inventory, Procurement, Kitchen and Sales import Accounting. Accounting imports
their read surfaces for the two workspaces and nothing else. No source module
imports an Accounting workspace.
*Enforced by:* `test_accounting_dependency_direction`.

---

## C. Manual journals

**C1 — the creator of a manual journal may not post it.**
*Enforced by:* `JournalEntry.created_by` + service check in
`post_journal_entry`; `verify_accounting` check `manual_maker_checker`.

**C2 — system-generated journals are exempt from C1 and read-only in
Accounting.**
A journal with a source identity is owned by the command that produced it. No
Accounting screen offers to edit, amend or discard one.
*Enforced by:* `source_event != ""` guard in every mutation command; the detail
template omits the controls rather than disabling them.

**C3 — an account whose `manual_posting_policy` is `FORBIDDEN` never carries a
manual line.**
Checked at draft validation **and again at post**, because a policy can change
between the two.
*Enforced by:* `services._validate_draft_shape` and `post_draft`.

**C4 — a manual journal has at least two lines, equal debits and credits, one
organization and one branch per line, a valid period, postable accounts, and a
cost centre wherever the account requires one.**
*Enforced by:* the kernel's existing `_validate_posting`, unchanged.

---

## D. Cash and bank

**D1 — one GL account backs at most one active cashbox or bank account per
organization.**
Two cashboxes on one account make both statements wrong and neither detectably
so.
*Enforced by:* partial `UniqueConstraint` on `(organization, account)` where
`is_active`, on each table, plus a cross-table service check.

**D2 — the GL account of a cashbox or bank account is postable, active and
belongs to the same organization.**
*Enforced by:* check constraint on postability via the `Account` row + service
validation; `verify_accounting` check `cash_account_consistency`.

**D3 — neither is ever hard-deleted.** Archived rows stay visible in history.
*Enforced by:* no delete route, no delete API, `on_delete=PROTECT` from the
documents that reference them.

---

## E. Expenses, accruals and prepayments

**E1 — an expense voucher's creator is not its approver, and its approver is
not required to be its poster but its creator never is.**
*Enforced by:* `approve_expense_voucher` and `post_expense_voucher` service
checks; `verify_accounting` check `expense_maker_checker`.

**E2 — a posted expense voucher and its lines are immutable.**
*Enforced by:* whole-row **allowlist** trigger (never a blocklist — see
`accounting/0005` for what forgetting one field cost).

**E3 — an expense voucher has no tax field and no supplier foreign key.**
Adding either would make it a second, weaker supplier-invoice path.
*Enforced by:* model shape; `verify_accounting` check
`expense_voucher_has_no_supplier_or_tax`.

**E4 — `Σ prepayment schedule line amounts == prepayment total`, exactly.**
Split with `apps/core/allocation.py`, never by rating each period and rounding.
*Enforced by:* check at creation; `verify_accounting` check
`prepayment_schedule_totals`.

**E5 — a posted schedule line is never rewritten when the master changes.**
Amending a prepayment re-plans only its `PLANNED` lines.
*Enforced by:* service; allowlist trigger on posted lines.

**E6 — nothing posts silently into a closed period.**
Amortization and accrual reversal both refuse a closed period and say so.
*Enforced by:* the kernel's `resolve_period` + `_validate_posting`.

**E7 — an accrual and the supplier invoice that replaces it never both
recognise the same expense.**
Linking an invoice does not create it; clearing the accrual is an explicit
command that reverses the accrual journal.
*Enforced by:* `clear_accrual` service; `verify_accounting` check
`no_duplicate_expense_recognition`.

---

## F. Periods and year end

**F1 — periods close in chronological order and reopen in reverse.**
*Enforced by:* existing `_validate_close_order` / `_validate_reopen_order`.

**F2 — a closed period refuses ordinary posting; a soft-closed period accepts
only an explicitly permitted correction.**
*Enforced by:* kernel `_validate_posting` + the two override permissions.

**F3 — the pre-close check reports every blocker at once and repairs none.**
A check that stops at the first blocker turns one close into six.
*Enforced by:* `period_close_blockers()` returns a list, and the screen renders
all of it.

**F4 — a fiscal year closes at most once while not reversed.**
*Enforced by:* partial `UniqueConstraint` on `(organization, fiscal_year)`
where `reversal_entry IS NULL`, plus the kernel's source-identity uniqueness on
the closing journal itself.

**F5 — reopening a closed fiscal year requires the exact reversal of its
closing journal first.**
*Enforced by:* `reverse_year_end_close` is the only path that clears the freeze.

---

## G. Reports

**G1 — total closing debit equals total closing credit, on every filter
combination.**
*Enforced by:* the report service computes both from the same line set;
`verify_accounting` check `trial_balance_balances`.

**G2 — `Assets = Liabilities + Equity` on every as-of date.**
Before year-end close, equity includes computed current-year earnings.
*Enforced by:* `verify_accounting` check `balance_sheet_equation`; the screen
shows a blocking finding and the difference when it fails.

**G3 — an unmapped account with a non-zero balance is shown in غير مصنف and
blocks final statement approval.**
Never silently omitted. An omitted balance is the one failure mode a reader
cannot detect.
*Enforced by:* the statement service returns unmapped rows as data, not as an
exception; `verify_accounting` check `unmapped_non_zero_accounts`.

**G4 — a running balance is computed in a service, never in a template.**
*Enforced by:* `test_no_running_balance_arithmetic_in_templates`.

**G5 — HTML and CSV read the same service.**
Two query paths drift, and the CSV is the one nobody looks at until an auditor
does.
*Enforced by:* the CSV views call the same functions the HTML views do.

**G6 — CSV cells are formula-neutralised and money is exact.**
*Enforced by:* `money_export` + the shared neutraliser; test
`test_csv_neutralises_formula_injection`.

---

## H. Scope

**H1 — permission plus scope, always (ADR-016).**
Out of scope → 404. In scope without authority → 403. A global Django
permission with no organization reach grants nothing.
*Enforced by:* `apps/organizations/authorization.py`; every Accounting view
resolves its object **with** the caller.

**H2 — an id submitted in a request can only select from what the caller
already reaches.**
*Enforced by:* `resolve_*` helpers; no Accounting view fetches then checks.

---

## I. Currency and tax

**I1 — IQD only. No FX, no multi-currency journal, no tax of any kind.**
*Enforced by:* absence of any rate, currency or tax field; `verify_accounting`
check `no_currency_or_tax_field`.
