# Task 5.0 — Accounting domain specification

- **Status**: Approved (owner decisions recorded, Section D of the Phase 5 prompt)
- **Date**: 2026-08-19
- **Branch**: `phase/5-accounting`, cut from `origin/phase/4-sales` at `53baa6c`
- **Related**: ADR-012 · ADR-013 · ADR-014 · ADR-015 · ADR-016 · ADR-017 ·
  ADR-019 · ADR-023 · ADR-027 · ADR-028, and the new ADR-029 / ADR-030 / ADR-031
- **Companion**: `docs/tasks/phase-5-task-breakdown.md`,
  `docs/invariants/accounting-module-invariants.md`

---

## 0. Base-commit note — certification must rebase

The Phase 4 completion tag `phase-4-sales-complete` **does not exist**. Per §A
of the Phase 5 prompt this branch was therefore cut from the latest clean
pushed `origin/phase/4-sales` (`53baa6c`, "fix(sales): correct findings from
the Phase 4 audit"), `phase/4-sales` was not reset or modified, and
implementation continued rather than stopping.

> **Blocking gate condition.** Final Phase 5 certification must rebase
> `phase/5-accounting` onto `phase-4-sales-complete` before the Phase 5 exit
> gate. Until that tag exists and the rebase happens, no Phase 5 completion
> claim is a certification claim.

The same note carried in `docs/tasks/task-4-0-sales-domain-spec.md` about the
missing `phase-3-kitchen-complete` tag is still open and still inherited here.

---

## 1. What Phase 5 is, and what it deliberately is not

Phases 0–4 built a ledger and four modules that post into it. Every journal in
the system so far was written **by** a source document: a goods receipt, a
supplier invoice, a production batch, a sales day. Nobody has ever been able to
open the ledger and read it.

Phase 5 is the module that reads it — and the small number of financial acts
that have no source document of their own: a manual correction, the electricity
bill, an expense accrued at month end, rent paid a quarter in advance, closing
a period, closing a year.

It is **not** a rewrite of the kernel. The kernel is `apps/accounting/models.py`
and `apps/accounting/services.py` as they stand: balanced entries, gapless
numbers, posted immutability, exact reversal, effective-dated role mappings,
monthly periods. Phase 5 adds screens, workspaces, four new document types, and
the reports. It changes no posting rule that Phases 1–4 already rely on.

### Out of scope, explicitly

HR · Payroll · a separate Treasury phase · Phase 6 · any Inventory, Kitchen or
Sales redesign · foreign exchange · multi-currency · VAT · sales tax ·
withholding tax · tax filing of any kind.

---

## 2. Audit of the existing implementation

Every section of the prompt, classified against what is actually on disk at
`53baa6c`. This is the classification Section C asked for, and the checkpoint
plan is derived from it.

| # | Section | Classification | Evidence |
|---|---------|----------------|----------|
| 1 | الأدوار المحاسبية | **Domain implemented, UI incomplete** | `AccountRole` + `SYSTEM_*_ROLES` (5 domains, 40 roles); `AccountRoleListView` + `role_list.html` exist but show a bare list — no domain/scope filter, no mapping count, no usage, no detail page |
| 2 | ربط الحسابات | **Substantially implemented** | `OrganizationAccountMapping`, EXCLUDE-gist overlap constraint, `create/amend/close/archive` services + commands + views + 4 templates. Missing: filters, history view, as-of preview, continuity warnings, HTMX fragments |
| 3 | دليل الحسابات | **Model exists, no UI at all** | `Account` with the C-GG-SS-AAA constraint pair and `create_account`/`archive_account` services. No route, no view, no template, no statement mapping |
| 4 | قيود اليومية | **Service exists, no UI, maker-checker absent** | `create_draft`/`update_draft`/`post_draft`/`post_entry`/`reverse_entry` + API endpoints. **`JournalEntry` has `posted_by` but no `created_by`** — creator ≠ poster is therefore not expressible today |
| 5 | الصناديق | **Genuinely absent** | No `Cashbox` anywhere in the repository |
| 6 | الحسابات البنكية | **Genuinely absent** | No `BankAccount` anywhere in the repository |
| 7 | ذمم الموردين | **Source data complete, no Accounting workspace** | `SupplierInvoice`, `SupplierCreditNote`, `SupplierPayment`, `PaymentAllocation`, `SupplierCreditAllocation`, `SupplierReturn` all posted and reconciled inside Procurement |
| 8 | ذمم التطبيقات | **Source data complete, no Accounting workspace** | `ApplicationReceivableEntry` (append-only), `DeliveryApplicationSettlement`, settlement adjustments — all in Sales |
| 9 | المصروفات | **Genuinely absent** | No `ExpenseVoucher` anywhere |
| 10 | المستحقات والمقدمات | **Genuinely absent** | No `Accrual`, no `Prepayment` anywhere |
| 11 | الفترات المحاسبية | **Service exists, no UI** | `FiscalYear`, `AccountingPeriod`, `soft_close_period`/`close_period`/`reopen_period`, close-order validation, a `PeriodCloseGuard` registry. No screen, no blocker report, no year-end close |
| 12 | ميزان المراجعة | **Partial selector, no UI** | `selectors.trial_balance` returns code/name/debits/credits only — no opening, no period, no closing, no filters, no CSV |
| 13 | دفتر الأستاذ | **Genuinely absent** | No ledger service, no running balance anywhere |
| 14 | قائمة الدخل | **Genuinely absent** | Also absent: any statement classification beyond `AccountClass` |
| 15 | الميزانية العمومية | **Genuinely absent** | — |
| — | Dashboard | **Genuinely absent** | Module `url_name` points at `accounting:mapping_list` |
| — | `verify_accounting` | **Genuinely absent** | Nine `verify_*` commands exist in other modules; accounting, which they all post into, has none |

Thirteen sidebar entries are inert (`*_sections(...)` in `apps/core/navigation.py`).
Two are active.

### What must not be duplicated

The audit found the following **already exist and are authoritative**. Phase 5
reads them and never re-implements them:

- the journal ledger — `JournalEntry` / `JournalLine`
- supplier liability — Procurement's invoice/credit-note/payment/allocation graph
- application receivable — Sales's `ApplicationReceivableEntry`
- stock value — Inventory's stock ledger
- production cost — Kitchen's batches and snapshots
- the account-role indirection — `AccountRole` + `OrganizationAccountMapping`

---

## 3. Owner decisions (Section D), recorded

### D1 — the kernel is authoritative

Preserved without change: Decimal-only money · 3-decimal stored IQD · exact
string JSON · balanced journals · branch-required lines · cost-centre policy by
account · effective-dated mappings · source identity · organization-scoped
idempotency · gapless numbers at posting · posted immutability · exact reversal
· monthly periods · soft close · close · reopen with reason · no period 13 ·
January fiscal-year start.

**No second journal ledger. No mutable account balance. Every balance in this
phase is derived from posted journal lines**, through
`apps/accounting/selectors.py`, at read time.

### D2 — data ownership

Accounting owns: chart of accounts · accounting roles · account mappings ·
manual journals · cashbox master · bank-account master · general expense
vouchers · accrual documents · prepayment schedules · accounting periods ·
financial statements · accounting verification.

Procurement keeps: `SupplierInvoice` · `SupplierCreditNote` · `SupplierPayment`
· supplier allocations · supplier returns.

Sales keeps: application receivable entries · sales posting · settlement ·
sales reversal · cashier closings.

Inventory keeps stock movements and stock value. Kitchen keeps production
posting and reversal.

Accounting screens **read and reconcile** those records. No Accounting screen
writes a Procurement, Sales, Inventory or Kitchen record. No Accounting model
duplicates one.

### D3 / D4 — the two subledger workspaces are read-only

`ذمم الموردين` and `ذمم التطبيقات` are reconciliation workspaces. Outstanding
supplier balance and outstanding application balance are **derived** — from the
source documents on one side and from the GL role account on the other, and the
whole point of the screen is to show that the two agree.

Forbidden: `SupplierBalance`, a mutable supplier outstanding, a second payment
allocation model, a mutable application balance.

A discrepancy is **reported and never repaired automatically**.

### D5 — cash and bank

`Cashbox` and `BankAccount` are master data. Each is tied to exactly one
postable GL account. Neither carries `current_balance` or any other stored
balance field. The balance shown on their pages is `account_balance(...)` over
posted lines, computed on request.

### D6 — manual journals

`DRAFT → POSTED → REVERSED`. **Creator must differ from poster.** System
journals are exempt because the source-domain command already owns their
maker-checker (a supplier invoice cannot be posted by the person who entered it
— Procurement enforces that at its own boundary).

A manual journal carries business date · branch · narration · reason/reference
· at least two lines · equal debits and credits · cost centres where the
account requires one · evidence · actor · immutable posting evidence.

`Account.manual_posting_policy` (new, §4.2) decides whether an account accepts a
manual line at all.

### D7 — currency and tax

IQD only. No FX gain/loss, no multi-currency journal, no VAT, no sales tax, no
withholding, no filing. **No Iraqi tax policy is invented anywhere in this
phase.**

### D8 — financial-statement mapping

`AccountClass` alone is insufficient: class `7` is "other income **and**
expense", and class `1` does not distinguish current from non-current. So
Phase 5 adds an explicit organization-owned `AccountReportMapping` with a
closed group set:

```
ASSET · LIABILITY · EQUITY · REVENUE · COST_OF_SALES ·
OPERATING_EXPENSE · OTHER_INCOME · OTHER_EXPENSE
```

plus a `presentation_section` for the balance-sheet split (current /
non-current) — see ADR-031.

No statement behaviour is derived from an account-code string comparison inside
a view. An unmapped account with a non-zero balance appears in a **غير مصنف**
section and blocks final statement approval. It is never silently omitted.

### D9 — current-year earnings

Before fiscal-year close the balance sheet carries a computed equity line
`CURRENT_YEAR_EARNINGS = YTD revenue − YTD expenses`, so that

```
Assets = Liabilities + Equity + Current Year Earnings
```

holds on any date. Income-statement accounts are **not** physically closed
every month.

### D10 — year-end close

No prior approved year-end policy exists in the repository, so the Release 1
rule of §D10 applies verbatim: every monthly period CLOSED · no unfinished
financial document · all reconciliations run · net income calculated · one
system-generated closing journal · revenue and expense zeroed · net result to
`RETAINED_EARNINGS` · unique once-only source identity · a recorded policy
version · the fiscal year frozen · exact reversal required before reopening.

Two roles are added because the current chart lacks them:
`CURRENT_YEAR_EARNINGS` and `RETAINED_EARNINGS`. No account id is hard-coded
anywhere; both resolve through `resolve_default_account`.

---

## 4. New model surface

Nine new models, one new role domain, four new account roles. Everything else
in Phase 5 is a screen, a selector or a report over what already exists.

### 4.1 New `AccountRoleDomain.ACCOUNTING`

The first domain whose posting rules are about the organization's **own**
financial administration rather than a trading module's. Filing an expense
accrual under `PURCHASING` because both involve a liability would make the
domain column a label rather than a fact — the same reasoning ADR-019 records
for `PURCHASING` and ADR-027 for `SALES`.

New roles, all `ORGANIZATION` scope:

| Role | Purpose |
|------|---------|
| `ACCRUED_EXPENSES_PAYABLE` | credit side of an accrual |
| `PREPAID_EXPENSE` | debit side of a prepayment, released by amortization |
| `CURRENT_YEAR_EARNINGS` | the computed equity line, and the closing journal's landing account |
| `RETAINED_EARNINGS` | where the closing journal leaves the result |

### 4.2 `Account` additions

Three fields, all with a safe default so no existing row changes meaning:

- `manual_posting_policy` — `ALLOWED` (default) / `RESTRICTED` / `FORBIDDEN`.
  `FORBIDDEN` refuses every manual line; `RESTRICTED` requires
  `post_restricted_manual_journal`. Control accounts that a subledger owns
  (`SUPPLIER_PAYABLE`, `DELIVERY_APP_RECEIVABLE`, `INVENTORY_CONTROL`) are
  seeded `RESTRICTED`: a manual credit to supplier payable silently breaks the
  subledger-to-GL equality that §L exists to prove.
- `is_system` — seeded accounts a user may not repurpose.
- `archived_at` — archived codes stay reserved; `is_active` already exists and
  keeps its meaning.

### 4.3 `AccountReportMapping`

`(organization, account)` unique, `statement_group` from the closed set,
`presentation_section` (`CURRENT` / `NON_CURRENT` / `NOT_APPLICABLE`),
`display_order`, `is_active`, history. Assigned to **postable** accounts.

### 4.4 `Cashbox` / `BankAccount`

Per §J and §K. Both: immutable `public_id` UUID · organization · code ·
Arabic/English name · exactly one postable GL account · active/archive ·
history. `Cashbox` adds branch (required) and `opened_on`; `BankAccount` adds
optional branch, bank name, **masked** account number, optional IBAN.

Partial unique constraint: one GL account backs at most one **active** cashbox
or bank account per organization. No stored balance field on either.

### 4.5 `ExpenseVoucher` / `ExpenseVoucherLine`

Per §N. `DRAFT → APPROVED → POSTED → REVERSED`, creator ≠ approver ≠ poster,
pay-from a `Cashbox` **or** a `BankAccount` (exactly one), lines carrying
account + cost centre + amount + deterministic sequence.

`Dr expense/asset lines · Cr the pay-from account's GL account`.

No tax field. No supplier FK. Not usable for supplier invoices, inventory
purchases, settlements, payroll, production or sales discounts — each of those
has its own document, and a second path to the same journal is a second version
of the truth.

### 4.6 `AccrualDocument` / `AccrualLine`

Per §O1. `Dr Expense · Cr ACCRUED_EXPENSES_PAYABLE`. Optional auto-reversal
date in the next open period. Optional link to the `SupplierInvoice` that
eventually arrives — **linking does not create the invoice**, and clearing the
accrual is an explicit command so the expense is recognised exactly once.

### 4.7 `Prepayment` / `PrepaymentScheduleLine`

Per §O2. Initial `Dr PREPAID_EXPENSE · Cr cash/bank`. Amortization
`Dr Expense · Cr PREPAID_EXPENSE`, one posted journal per schedule line.

Schedule amounts are split with `apps/core/allocation.py` (certified
largest-remainder, remainder DESC then sequence ASC) so that
`Σ schedule lines == total` **exactly**, with no rounding residue. Line
lifecycle `PLANNED → POSTED → REVERSED`. A posted line is never rewritten when
the master record changes, and nothing posts silently into a closed period.

### 4.8 `YearEndClose`

The once-only record of §D10: organization, fiscal year, net result, policy
version, the closing `JournalEntry`, the reversal entry if reopened, actor,
evidence. Unique per `(organization, fiscal_year)` while not reversed.

---

## 5. Reports

All four read `posted_lines(...)` and nothing else.

**ميزان المراجعة** — opening debit/credit, period debit/credit, closing
debit/credit per account, filtered by branch · fiscal year · date range · cost
centre · account range · account class · include-zero · before/after closing
entries. `Σ closing debit == Σ closing credit`, always.

**دفتر الأستاذ** — ordered `business date → posted_at → entry number → line
number`, with a running balance that follows the account's normal balance.
**The running balance is computed in the service, never in a template**, and
the same service feeds HTML and CSV.

**قائمة الدخل** — Revenue · Cost of Sales · Gross Profit · Operating Expenses ·
Operating Profit · Other Income · Other Expenses · Net Profit. Statement
mapping driven. No taxes.

**الميزانية العمومية** — as of an explicit date, current/non-current split,
equity including computed current-year earnings before close and retained
earnings after. If `Assets ≠ Liabilities + Equity` the page shows a **blocking
finding with the difference** and repairs nothing.

CSV everywhere: exact Decimal through `money_export`, and every cell that could
start `=`, `+`, `-` or `@` is neutralised.

---

## 6. Permissions

Twenty-three permissions across the module, of which thirteen already exist and
keep their names and scope. The new ones follow §V. Scope discipline is
unchanged: permission **plus** scope, out-of-scope 404, in-scope without
authority 403, a global Django permission with no organization reach grants
nothing.

Structural and organization-wide acts (chart, mappings, periods, year-end,
statements) are `ORGANIZATION` scope. Acts that name a branch (journal lines,
expense vouchers, cashbox reads) are `BRANCH` scope.

`ACCOUNTANT` creates and edits drafts and reads reports but never reopens a
period, never approves their own document, and never closes a year.
`STOREKEEPER`, `PURCHASING` and `CASHIER` hold no Accounting write authority at
all.

---

## 7. Acceptance

Phase 5 is complete when all fifteen sidebar entries are active with zero
قريباً badges, every full page returns 200, every HTMX route returns real
fragment markup with no nested shell, every screen shows demo data, the trial
balance balances, the balance sheet balances, `verify_accounting` reports zero
ERROR findings, the accounting demo is idempotent across two runs, and
`pytest apps/accounting` passes.

The complete project suite is **deferred to the Phase 5 exit gate by owner
policy** (§B), together with the rebase onto `phase-4-sales-complete` recorded
in §0.
