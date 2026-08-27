# Financial-control rollout plan

This plan turns the current reconciliation warnings into durable controls
without rewriting historical transactions or inventing Iraqi tax, currency, or
precision rules.  It is ordered by dependency: a later approval or report must
never claim a source is complete before the underlying evidence exists.

## Current baseline — do not "repair" by journal entry

The current review figures are a diagnostic baseline, not conversion data:

- net sales: IQD 75,719,000;
- application sales: IQD 35,566,550 while declared application receivable is
  zero;
- around IQD 33,876,000 in cash exposure and no CashierShift for posted sales
  days;
- 131 sales lines without cost coverage, so COGS is currently zero;
- reported income of about IQD 15,312,160 is provisional;
- no payroll/employee evidence despite IQD 27,000,000 of salary expense;
- no cashbox or bank account, 22 unmapped roles, 12 open periods, and
  fractional-IQD residues.

No migration backfills actors, shifts, declared tenders, COGS, bank balances,
or payroll documents.  The evidence does not exist, so inventing it would make
the audit trail look better while making it less true.

## Phase 1 — stop new incomplete sales postings

Status: implemented for each organization from the deployment date recorded in
`AccountingSettings.daily_close_enforced_from`.

1. The SalesDay maker submits a frozen day; the same actor cannot post it.
   The service and database constraint both enforce this.
2. A cashier may close against a **submitted** day because its figures are
   frozen.  This moves the count before, rather than after, the ledger event.
3. The close captures declared versus derived cash/card/application amounts,
   cash-count and card-count variances, and missing/open-shift findings in an
   immutable `DailyFinancialClose` attempt.
4. A different reviewer can approve only a clean attempt.  A blocked attempt
   remains evidence; correcting the source creates the next attempt.
5. A controlled SalesDay cannot post unless its latest close is approved.

Acceptance evidence:

- clean path: submitted day → closed cashier shift → submitted close →
  independent approval → post;
- no-close, self-review, self-post, tender mismatch, cash variance, and card
  variance all refuse posting;
- raw SQL/queryset update cannot rewrite an approved/blocked close;
- existing posted days remain readable and unchanged.

## Phase 2 — cost coverage and honest profitability

Dependency: Phase 1 posted sales identity and date scope.

1. Create a date-effective COGS coverage ledger that links each posted sales
   line to the authoritative recipe-cost snapshot or direct-stock issue used.
2. Generate coverage exceptions for missing snapshot, missing valuation,
   unavailable recipe/serving, and uncosted direct-stock consumption.  Keep
   source identifiers, snapshot id, warehouse, and effective date on every
   row.
3. Make the income statement, dashboard, and exports explicitly
   `PROVISIONAL` whenever coverage is incomplete.  Show covered revenue,
   uncovered revenue, covered COGS, and the line count; never call missing COGS
   zero.
4. Require finance review of a cost-coverage release before the related
   period can close.  Release reasons and actors must be append-only.

Exit measure: 100% of the selected reporting window is either covered by
authoritative cost evidence or named in an unresolved exception report.

## Phase 3 — application receivables, cash, bank, and AP

Dependencies: mapped control accounts, source identity, and Phase 1 daily
close evidence.

1. Add a delivery-application settlement-period model with imported/attached
   statement reference, gross/commission/fees/net terms, and a reconciliation
   to the sales receivable ledger.  Do not fabricate a statement when none is
   available.
2. Add cashbox and bank-account masters, deposit/transfer evidence, and
   daily cash-to-bank reconciliation.  Cash and card are reported separately;
   card batches are introduced only when acquirer evidence is available.
3. Add AP invoice and payment maker-checker: creator ≠ approver/poster,
   supplier subledger reconciliation, allocation evidence, and stale/due
   warnings.  Supplier catalog/pricing remains optional operational data, not
   an accounting source.

Exit measure: every application balance, cashbox balance, bank balance, and
supplier payable report reconciles to an identified document population and
shows its exceptions.

## Phase 4 — operating expenses and payroll evidence

Dependencies: Phase 3 cash/bank and approved account-role mappings.

1. Require an expense voucher or accrual source, payment allocation, branch
   and cost-center attribution, creator/approver separation, and a reason for
   manual or late postings.
2. Link payroll journals to approved payroll runs, employees/contracts, and
   payment evidence.  Until HR records exist, salary expense remains an
   explicit reporting exception, not inferred payroll.
3. Add monthly operating-expense variance reports by account, branch and cost
   center; label any missing comparative basis rather than inventing a budget.

## Phase 5 — period close and management reporting

Dependencies: Phases 1–4, complete mappings, and explicit policy decisions.

1. Add a close-readiness gate that aggregates unmapped roles, unreconciled
   daily closes, receivable/AP/cash/bank exceptions, incomplete COGS, payroll
   evidence gaps, and fractional-IQD precision exceptions.
2. Soft close blocks ordinary postings; final close requires independent
   reviewer sign-off and creates an append-only closing record.  Reopen only
   through a reasoned, audited authority.
3. Deliver the management pack: P&L, COGS and gross margin, operating
   expenses, net result, sales by channel, application receivables, cash and
   bank, payables, inventory/production variances, and close-readiness status.

The pack must say **provisional** until the selected period reaches the
approved coverage/reconciliation thresholds.  It must not create a tax amount,
rounding rule, or statutory IQD precision policy; those require owner and tax
advisor confirmation first.
