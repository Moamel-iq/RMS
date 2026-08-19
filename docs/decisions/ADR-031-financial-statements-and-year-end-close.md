# ADR-031 — Financial-statement mapping, current-year earnings, and year-end close

- **Status**: Accepted
- **Date**: 2026-08-19
- **Task**: 5.0 — Accounting domain specification, and Phase 5 checkpoints 7–9
- **Related**: ADR-012 (monetary precision), ADR-013 (fiscal year and periods),
  ADR-014 (chart of accounts), ADR-017 (source identity and idempotency),
  ADR-019 (account roles)
- **Companions**: ADR-029 (accounting operations), ADR-030 (cash, bank,
  expenses and deferrals)

---

## 1. The account class is not a statement classification

**Decision.** Phase 5 adds `AccountReportMapping` — an explicit,
organization-owned statement classification per postable account — rather than
deriving statement placement from the account code.

`AccountClass` was designed in ADR-014 to carry the *first segment of the
code*, and it does that job correctly. It cannot carry statement placement:

- Class `7` is "إيرادات ومصروفات أخرى" — **both** other income and other
  expense. Cash rounding gain/loss, count variance, settlement variance and
  cash over/short all live there, and each can land on either side of the
  income statement depending on its sign. A statement cannot ask a class-7
  account which section it belongs to.
- Class `1` is "الأصول" with no current / non-current distinction. Inventory,
  cash and supplier advances are current; a delivery vehicle is not. The
  balance sheet needs that split and the code does not encode it.
- Class `8` is clearing. GRNI is a real liability that belongs on the balance
  sheet; inter-branch clearing nets to zero across branches and is presentation
  noise at organization level.

**The alternative that was rejected** is a code-prefix check inside the
statement view — `if account.code.startswith("4")`. Three reasons it loses.
It hides financial-statement behaviour in a view where nobody looks for it; it
breaks the moment a second organization numbers its chart differently, which
ADR-014 explicitly allows; and it cannot express the class-7 split at all
without a second, longer prefix table that is a mapping in denial.

The group set is closed: `ASSET`, `LIABILITY`, `EQUITY`, `REVENUE`,
`COST_OF_SALES`, `OPERATING_EXPENSE`, `OTHER_INCOME`, `OTHER_EXPENSE`. A
`presentation_section` of `CURRENT` / `NON_CURRENT` / `NOT_APPLICABLE` carries
the balance-sheet split.

## 2. An unmapped balance is shown, never omitted

**Decision.** A postable account with a non-zero balance and no report mapping
appears in a **غير مصنف** section on both statements, and blocks final
statement approval.

This is the single most important decision in this ADR, because the natural
implementation is the opposite. A statement built by iterating mappings
produces a beautiful, balanced, **wrong** report when an account is
unmapped — the balance simply is not there, nothing indicates its absence, and
the income statement's own arithmetic still ties because every line in it is
internally consistent.

A missing balance is the one error a reader cannot detect from the report. So
the service resolves the account set from **the ledger**, not from the mapping
table, and any account it cannot classify becomes a visible row.

System clearing accounts may be excluded from presentation only when their
balance is zero, or when the approved report policy places them in the
appropriate asset or liability section. "Excluded because it is a clearing
account" is not available for a non-zero balance.

---

## 3. Current-year earnings is computed, not posted monthly

**Decision.** Before fiscal-year close, the balance sheet carries a computed
equity line:

```
CURRENT_YEAR_EARNINGS = year-to-date revenue − year-to-date expenses
```

so that `Assets = Liabilities + Equity + Current Year Earnings` holds on any
date, in an ordinary open month, with no closing entry posted.

**Why not close income-statement accounts every month.** Because monthly
closing entries destroy the year-to-date income statement. Once March's revenue
has been swept to equity, "revenue for the year to date" has to be
reconstructed from closing journals rather than read from the accounts, every
report becomes period-scoped, and comparatives across a close boundary stop
meaning what they say. The computed line costs one aggregate query and leaves
the revenue and expense accounts carrying their real cumulative figures all
year.

The line is computed from the **statement mapping**, not from the class codes,
for the reasons in §1 — a class-7 account's contribution depends on which group
it maps to.

---

## 4. Year-end close is one journal, once, and reversible

**Decision.** The Release 1 rule of §D10, since no prior approved year-end
policy exists in the repository.

Preconditions, all of them, checked together and reported together:

- every monthly period in the fiscal year is `CLOSED`
- no active draft or unfinished financial document remains
- every accounting reconciliation runs clean

Then: net income is calculated, **one** system-generated closing journal is
created, revenue and expense accounts are zeroed, the net result is transferred
to `RETAINED_EARNINGS`, and the fiscal year is frozen.

**Once-only** is enforced twice over. The closing journal carries a source
identity — `ACCOUNTING.YEARENDCLOSE / <fiscal year id> / POSTED` — and
ADR-017's per-organization uniqueness on source identity makes a second one
impossible at the database. `YearEndClose` additionally carries a partial
unique constraint on `(organization, fiscal_year)` where the reversal is null.

> **Implementation note, learned the expensive way in Phase 4.**
> `canonical_source_identity` case-folds `source_document_type` to
> **upper case** before persisting it. A constant spelled
> `"accounting.YearEndClose"` writes `ACCOUNTING.YEARENDCLOSE` and then fails
> to find itself again — a reversal that cannot locate its own journal. Write
> the constant in the stored form.

**A policy version is recorded** on the close, so a year closed under one set
of rules stays interpretable after the rules change.

**Reopening requires exact reversal first.** `reverse_year_end_close` posts the
mirror of the closing journal, unfreezes the year, and leaves both entries in
the ledger. Deleting the closing journal is not available, and neither is
editing it — it is posted, and posted journals are immutable by trigger.

## 5. Consequences

Two new account roles, `CURRENT_YEAR_EARNINGS` and `RETAINED_EARNINGS`, and
their chart accounts under equity. Both resolve through
`resolve_default_account`; **no account id is hard-coded**.

After close, the balance sheet's equity is carried by retained earnings
according to the closing journal, and current-year earnings for the closed year
is zero — not because it is suppressed, but because the accounts it sums are
genuinely zero. The same computation serves both sides of the close, which is
what makes the two views reconcile.

---

## 6. Reports read one service, and compute nothing in a template

**Decision.** Every report — trial balance, ledger, income statement, balance
sheet — is a service function returning exact `Decimal`s, and both the HTML
view and the CSV view call the same function.

Two query paths drift. The CSV is the one nobody looks at until an auditor
does, which is the worst possible moment to discover that it disagrees with the
screen.

**A running balance is computed in the service, never in a template.** Template
arithmetic cannot carry a `Decimal` accumulator across rows without a filter
that hides the ordering assumption, and the ordering is the whole content of a
running balance: `business date → posted_at → entry number → line number`.
Getting that order wrong produces a column that is individually plausible on
every row and wrong in total.

CSV money goes through `money_export` — exact, 3 dp, ungrouped,
locale-independent — and every cell that could begin `=`, `+`, `-` or `@` is
neutralised before it reaches a spreadsheet.
