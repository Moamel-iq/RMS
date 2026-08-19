# Accounting operations — the documents Phase 5 owns, and what each one posts

**Status:** current as of Phase 5 completion, 2026-08-19.
**Decisions:** ADR-029 (accounting operations and manual journals), ADR-030
(cash, bank, expenses and deferrals), ADR-031 (financial statements and
year-end close). Where this document and an ADR disagree, the ADR wins and this
document is wrong.

This is the reference for the five things Accounting itself writes: manual
journals, cashbox and bank master records, expense vouchers, accruals and
prepayments. Everything else the module shows — supplier liabilities,
application receivables, the ledger, the statements — is a **read** over
documents other modules own, and lives in
[`accounting-reports.md`](accounting-reports.md).

---

## The one rule underneath all of it

**There is no second ledger and no stored balance anywhere in this module.**

Every figure the Accounting module displays — a cashbox position, a bank
position, a supplier's outstanding, a trial-balance line, a balance-sheet
total — is derived from posted journal lines at the moment it is asked for.
Nothing is cached in a column that a later write could forget to update.

This is why `Cashbox` and `BankAccount` carry no `current_balance`, why the
supplier and application workspaces store nothing at all, and why the
verifier's job is comparison rather than repair. A stored balance is a second
answer to a question that already has one, and the two disagree silently.

---

## الأدوار المحاسبية · ربط الحسابات — roles and mappings

An accounting **role** is a name for a purpose (`ACCRUED_EXPENSES_PAYABLE`,
`PREPAID_EXPENSE`, `CURRENT_YEAR_EARNINGS`). A **mapping** says which account
carries that role in one organization, over a date range.

Every service in this module resolves its accounts through
`resolve_default_account(organization=…, account_role=…, on_date=…)`. **No
account code is written into any service in `apps/accounting/`.** A code in a
service is a code that has to be found and changed in every deployment whose
chart differs, and one that will be missed.

Mappings are effective-dated and versioned. Correcting a mapping nothing has
posted through is an *amend*; correcting one that has been used is a *close*
followed by a new mapping from the next day. A used mapping is never rewritten,
because the journals that went through it would then cite a mapping that no
longer says what it said when they posted.

| Act | Screen | Endpoint |
|---|---|---|
| Map a role from a date | ربط الحسابات | `POST /api/v1/account-role-mappings/` |
| Correct an unused mapping | the amend form | `PATCH /api/v1/account-role-mappings/{id}/` |
| End a used mapping's range | the close form | `POST /api/v1/account-role-mappings/{id}/close/` |
| Withdraw one recorded in error | the archive action | `POST /api/v1/account-role-mappings/{id}/archive/` |

---

## قيود اليومية — manual journal entries

`DRAFT → POSTED → REVERSED`. Three properties are enforced by the kernel, not
by the screen:

**The creator may not post their own entry.** `validate_manual_maker_checker`
refuses it whoever holds the permissions, and the API is refused for the same
reason the screen is. System-generated journals — the ones Procurement, Sales
and Kitchen produce — are exempt, because there is no second human in an
automatic posting and pretending otherwise would mean nobody could post a sale.

**A restricted account refuses a hand-written line.** Control accounts
(`2-01-01-001` and its siblings) carry `manual_posting_policy = RESTRICTED`
precisely so a manual entry cannot land in the subledger's control account and
break the reconciliation that account exists to support. Posting into one needs
`accounting.post_restricted_manual_journal` and is deliberate.

**A posted entry is immutable, by trigger.** Migration `accounting/0005`
compares the whole row (`%ROWTYPE`) against an allowlist of permitted changes
rather than blocking a list of forbidden columns. A blocklist has to be
remembered every time a column is added; an allowlist covers new columns
automatically. Corrections are reversals, never edits.

---

## الصناديق · الحسابات البنكية — cash and bank master records

A cashbox or bank account is a **name for a GL account**, plus the branch that
holds it and the date it came into use. Registering one does not create money;
it creates the record that expense vouchers, prepayments and the sales module
credit when they pay from it.

One account, one active record: a partial unique index on
`(organization, account) WHERE is_active` stops the same GL account being
described by two live cashboxes, which would make "the balance of this cashbox"
ambiguous in exactly the way the no-stored-balance rule is meant to prevent.

Bank account numbers are stored **masked**. The full number is not needed to
reconcile a statement and is a liability to hold.

**Reconciliation is a date stamp, not a balance.** `POST
/api/v1/cash-records/{kind}/{record_id}/reconcile/` records that a human
compared the record to its statement on a date. It changes no figure, because
there is no figure stored to change.

---

## المصروفات — expense vouchers

A non-supplier operational expense, paid immediately: the electricity bill, a
taxi, a municipal fee, a repair paid in cash. `DRAFT → APPROVED → POSTED →
REVERSED`.

The journal is `Dr expense lines · Cr the pay-from account`, and the credit
side comes from the `Cashbox` or `BankAccount` record rather than from a GL
account named directly — so the voucher's cash effect appears on that record's
statement with no second place to keep in step.

Two absences are the design, not an omission:

**No supplier field.** The moment a voucher can name a supplier it becomes a
supplier invoice with no three-way match, no GRNI clearing, no purchase price
variance and no credit-note path — and it will be used as one, because it is
faster. Supplier invoices belong to Procurement.

**No tax field.** Release 1 has no approved Iraqi tax policy. A field labelled
"ضريبة" would invite one to be invented per voucher by whoever filled it in.

An **unpaid** expense is not a voucher. It is an accrual.

The creator may neither approve nor post their own voucher. Authoring is
branch-scoped (`manage_expense_vouchers`); approving and posting are
organization decisions (`approve_expense_vouchers`).

---

## المستحقات — accruals

Recognise a cost before its paperwork arrives: `Dr Expense · Cr
ACCRUED_EXPENSES_PAYABLE`, the liability resolved by role.

Reversing an accrual can record **which invoice superseded it**, and recording
that link is not the same as creating the invoice. Accounting never writes a
supplier invoice — that document belongs to Procurement and arrives through
Procurement. The link plus the reversal is what makes the expense stand
recognised exactly once (ADR-030 §4).

---

## المقدمات — prepayments

Recognise a payment before the cost is consumed: `Dr PREPAID_EXPENSE · Cr
cash/bank` when paid, then one `Dr Expense · Cr PREPAID_EXPENSE` per schedule
line as each period is consumed.

**The schedule is split with `apps/core/allocation.py`, never by dividing the
total by the period count.** This is the ADR-006 counterexample in a different
costume: 1,000,000 over three months at three decimal places is 333,333.333
each, which sums to 999,999.999. The residual is one thousandth of a dinar and
it is fatal — the prepaid account never reaches zero, the balance sheet carries
a permanent 0.001 asset, and the account cannot be closed at year end without a
plug.

`end_date` is **derived**, never submitted. It is the end of the last period as
computed by the same `_period_bounds` that dates the schedule lines. A
caller-supplied end date could disagree with the final line by a day, and the
balance sheet and the schedule would then describe two different assets.

Posting one instalment into a closed period is **refused, and the refusal names
the period**. The accountant reopens it or posts a catch-up deliberately; what
must not happen is the system quietly choosing the current month.

---

## الفترات المحاسبية — periods

`OPEN → SOFT_CLOSED → CLOSED`, with reopening behind its own permission and
its own audit event recording the authority that allowed it.

**`فحص ما قبل الإغلاق` is a report, not an attempt.** The kernel's
`_run_period_close_guards` raises on the *first* veto, which is right for a
close and useless for a preview. `period_close_blockers` collects every blocker
instead — drafts, unposted vouchers, unposted accruals, due prepayment
instalments, unmapped roles, out-of-order periods — so an accountant clearing a
month sees the whole list at once. That is the difference between one afternoon
and six.

A guard that *raises unexpectedly* is reported as an advisory finding rather
than aborting the preview. The other answers are still worth having.

---

## The API

Everything above is reachable over HTTP under `/api/v1/`. The endpoints are
named after accounting acts — `approve`, `post`, `reverse`, `close` — and there
is no `PUT` on anything that has left `DRAFT`. A journal that could be `PUT` is
a journal that could be rewritten.

**Money crosses the boundary as a string in both directions.** JSON has one
numeric type and it is binary floating point, so a bare `1250.001` in a request
body would already have been through a float before any Python code saw it. A
string cannot be rounded by a parser.

The write path is `api_* → commands → services → kernel`. The API never calls
`services.py` directly and never calls `Model.objects.create` on ledger state.

| Area | Module | Prefix |
|---|---|---|
| Journals and periods | `apps/accounting/api.py` | `/journal-entries/`, `/periods/` |
| Chart, roles, mappings, cash | `apps/accounting/api_master.py` | `/accounts/`, `/account-roles/`, `/cashboxes/`, `/bank-accounts/` |
| Expenses, accruals, prepayments | `apps/accounting/api_documents.py` | `/expense-vouchers/`, `/accruals/`, `/prepayments/` |
| Statements, subledgers, pre-close | `apps/accounting/api_reports.py` | `/reports/`, `/subledgers/`, `/periods/{id}/pre-close/` |

Out of scope is **404**; in scope without authority is **403**. A 403 about
another organization's record would confirm it exists, and ids are sequential
(ADR-016).

---

## What Phase 5 deliberately does not do

* **No second journal ledger.** The Phase 0 kernel is authoritative.
* **No mutable balances**, on any model, anywhere in the module.
* **No supplier balance table and no second allocation model.** The supplier
  and application workspaces are read/reconciliation surfaces; a discrepancy is
  reported and never repaired automatically (ADR-029 §3).
* **No FX, no multi-currency.** IQD only.
* **No VAT, no sales tax, no withholding, no tax filing.** There is no approved
  Iraqi tax policy to implement, and inventing one would be worse than the gap.
* **No payroll.** That is Phase 6.
