# Phase 5 — Accounting task breakdown

Eleven checkpoints. Each is one commit, pushed before the next begins.

The ordering is not cosmetic. Every later checkpoint reads something an earlier
one created: journals need the chart, cash needs postable accounts, expenses
need cashboxes, the statements need the report mapping, and the verifier needs
all of it. Reordering them means building against a hole.

---

## Checkpoint 0 — Domain specification

`docs/tasks/task-5-0-accounting-domain-spec.md` ·
`docs/tasks/phase-5-task-breakdown.md` ·
`docs/invariants/accounting-module-invariants.md` ·
ADR-029 · ADR-030 · ADR-031.

Records the Section D owner decisions and the audit classification of every
existing accounting file.

> `docs(accounting): approve Phase 5 Accounting domain specification`

---

## Checkpoint 1 — Roles, mappings and the chart

**Sections** الأدوار المحاسبية · ربط الحسابات · دليل الحسابات

- `AccountRoleDomain.ACCOUNTING` + four roles (`ACCRUED_EXPENSES_PAYABLE`,
  `PREPAID_EXPENSE`, `CURRENT_YEAR_EARNINGS`, `RETAINED_EARNINGS`) and their
  chart accounts.
- `Account.manual_posting_policy` / `is_system` / `archived_at`.
- `AccountReportMapping` and the seeded statement groups.
- Role list with domain/scope/mapped filters, mapping counts, usage; role
  detail with effective mappings, history and unresolved organizations.
- Mapping list gains filters, history, as-of preview, continuity warnings.
- Chart of accounts: tree with lazy HTMX branch loading, flat list, create,
  edit-allowed-metadata, archive/reactivate, detail with balance and ledger
  drill-down.

> `feat(accounting): complete roles mappings and chart of accounts`

## Checkpoint 2 — Journals

**Section** قيود اليومية

- `JournalEntry.created_by` + the creator ≠ poster rule, system journals exempt.
- Journal list with the full §I filter set; manual draft create/edit/line
  editing through HTMX; submit/post; reverse; detail with source-document link
  and audit timeline; trial-balance impact preview.
- `manual_posting_policy` enforced at draft validation and again at post.

> `feat(accounting): complete journal entry operations`

## Checkpoint 3 — Cash and bank

**Sections** الصناديق · الحسابات البنكية

- `Cashbox`, `BankAccount`, one postable GL account each, no stored balance.
- Statement service: opening / debit / credit / closing with a running balance,
  ordered `business date → posted_at → entry number → line number`.

> `feat(accounting): add cashbox and bank account workspaces`

## Checkpoint 4 — Subledgers

**Sections** ذمم الموردين · ذمم التطبيقات

- Supplier workspace over Procurement source data: balances, open invoices,
  credit notes, payments, allocations, returns, aging on invoice due-date
  snapshots, statement, GL reconciliation.
- Application workspace over Sales receivable entries: opening, posted, sales
  reversals, settlements, adjustments, closing, aging, statement, GL
  reconciliation.
- Both read-only. Discrepancies reported, never repaired.

> `feat(accounting): add supplier and application reconciliation`

## Checkpoint 5 — Expenses

**Section** المصروفات

`ExpenseVoucher` + `ExpenseVoucherLine`, `DRAFT → APPROVED → POSTED →
REVERSED`, maker-checker, pay-from cashbox or bank, posting and reversal, full
Arabic screens, API.

> `feat(accounting): add expense voucher workflow`

## Checkpoint 6 — Accruals and prepayments

**Section** المستحقات والمقدمات

`AccrualDocument`/`AccrualLine` and `Prepayment`/`PrepaymentScheduleLine`, the
largest-remainder schedule split, amortization posting, the accrual-to-invoice
link and the clearing command that stops double recognition.

> `feat(accounting): add accrual and prepayment schedules`

## Checkpoint 7 — Periods and year end

**Section** الفترات المحاسبية

Fiscal-year and period screens, states and transitions with reasons, the
one-shot **فحص ما قبل الإغلاق** blocker report, `YearEndClose`, the closing
journal and its exact reversal.

> `feat(accounting): complete period and fiscal year controls`

## Checkpoint 8 — Core reports

**Sections** ميزان المراجعة · دفتر الأستاذ

Both filter sets, both CSV exports, one shared ledger service for HTML and CSV,
running balance computed in Python and never in a template.

> `feat(accounting): add trial balance and general ledger`

## Checkpoint 9 — Financial statements

**Sections** قائمة الدخل · الميزانية العمومية

Mapping-driven sections, the four income-statement formulas, the balance-sheet
equation with computed current-year earnings, comparatives, drill-down, CSV,
and the غير مصنف section that blocks approval.

> `feat(accounting): add financial statements`

## Checkpoint 10 — Dashboard and module completion

Accounting landing page with independently-loading HTMX panels ·
`seed_accounting_demo` · `verify_accounting` · navigation activation for all
fifteen entries · documentation · `pytest apps/accounting` · focused
integrations.

> `feat(accounting): complete Accounting operational module`

---

## Fast checks per checkpoint (§AB)

```
python manage.py migrate
python manage.py check
python manage.py makemigrations --check --dry-run
ruff check <changed paths> ; ruff format --check <changed paths>
mypy <changed modules>
```

plus an authenticated smoke proving: full page 200 · HTMX fragment 200 ·
**fragment carries real markup** · no nested shell · route active · demo data
visible · allowed action works · forbidden action refused · Decimal quoted · no
duplicate demo rows.

> The fragment check asserts **content**, not merely status. A fragment smoke
> that only checks `200`, "no nested `<html>`" and "shorter than the page" is
> satisfied by an empty body — which is exactly how nine screens shipped
> blank in Phase 4 and went unnoticed until an audit opened one.

The complete project suite is **deferred to the Phase 5 exit gate by owner
policy** (§B).
