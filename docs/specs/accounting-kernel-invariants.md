# Accounting kernel — enforced invariants (Task 0.6)

The approved checklist Task 0.6 must satisfy. Each line is a test, not a
guideline. Nothing in this list is optional and none of it may be relaxed to
make a suite pass.

**Status: all twelve enforced and tested as of Task 0.6.**

| # | Invariant | Enforced where | Test |
|---|---|---|---|
| 1 | Journal entry debits equal credits, on **stored 3-decimal values** | `validate_balanced` + deferred constraint trigger `accounting_journalline_balance` | `TestBalance` |
| 2 | No float anywhere in an accounting calculation | `apps/core/money.py::ensure_decimal` | `test_no_float_may_reach_an_amount` |
| 3 | No posting to a non-leaf account | `validate_accounts_are_postable` + trigger `accounting_journalline_account_postable` | `TestPostableAccounts` |
| 4 | No posting to a `CLOSED` period | `validate_period_accepts_postings` | `TestPeriods` |
| 5 | Every journal line belongs to exactly one branch | Non-null FK on `JournalLine.branch` | `TestOrganizationIsolation` |
| 6 | Cost centers required per `Account.requires_cost_center` | `validate_cost_centers` | `TestCostCenterPolicy` |
| 7 | All allocation is deterministic | `apps/core/allocation.py` | `test_allocation.py` |
| 8 | Document total equals the sum of its stored posted lines | `entry_total`, trial balance | `TestTrialBalance` |
| 9 | UI rounding never affects ledger values | Renderers return `str` | `TestRendering` |
| 10 | Accounts, cost centers, branches used by posted journals cannot be deleted | `on_delete=PROTECT` | `TestArchivingNotDeleting` |
| 11 | Posted journals are immutable; corrections are reversals | Triggers `accounting_journalentry_no_change`, `accounting_journalline_no_change` | `TestImmutability`, `TestReversal` |
| 12 | Posting, reversal, and period reopening are audit logged | `record_audit_event` in every service | `test_reopening_is_audited_with_its_reason`, `test_reversal_is_audited` |

### Hardening pass (approved after the first Task 0.6 review)

| Rule | Where | Test |
|---|---|---|
| An account that has received posted lines can never become a parent | `validate_parent_has_no_posting_history`, called from `create_account` | `TestHierarchyExclusivity` |
| An account with children can never receive a line | `validate_accounts_are_postable` | `test_an_account_with_children_cannot_receive_a_line` |
| An entry needs at least one debit **and** one credit line | `validate_both_sides_present` | `TestEntryShape` |
| A balanced entry of zero is not a valid entry | `validate_entry_has_value` | `test_a_two_line_zero_value_entry_is_refused` |
| Every line amount is strictly positive | `validate_line_sides` | `test_every_line_amount_must_be_positive` |
| Closing is chronological: Jan → Feb → Mar | `_validate_close_order` | `TestPeriodOrdering` |
| Reopening is reverse-chronological: Mar → Feb → Jan | `_validate_reopen_order` | `test_a_period_cannot_reopen_while_a_later_one_is_closed` |
| Fiscal-year closure is **derived**, never stored | `FiscalYear.is_closed` property | `TestFiscalYearClosureIsDerived` |
| A second reversal reports `already_reversed`, not `not_posted` | `reverse_entry` checks the relationship first | `TestReversalErrorAccuracy` |
| The deferred balance trigger fires at a real COMMIT | `test_commit_boundary.py` (`transaction=True`) | `test_an_unbalancing_line_is_refused_at_commit` |

**Hierarchy exclusivity is structural first.** Only a four-segment detail code
is postable, and no valid code extends one, so a postable account cannot
acquire a child and a parent cannot become postable. The explicit checks are
the second lock, so a future change to the code scheme cannot quietly open the
hole.

**`is_adjustment` carries no date rule.** A month-end adjustment is a
legitimate accounting act. A year-end adjustment is simply
`is_adjustment=True` with `accounting_date = fiscal_year.end_date`. There is
deliberately no "adjustments must be dated at year end" constraint.

**Soft-close semantics.** `OPEN` — normal rules. `SOFT_CLOSED` — routine
posting blocked; specifically-authorized adjustments and reversals allowed.
`CLOSED` — nothing posts or reverses. **The authorization that gates the
soft-closed path does not exist yet**; today the capability is open to any
caller. Task 0.7 supplies it.

**Both balance tests are kept on purpose.** The `SET CONSTRAINTS ALL
IMMEDIATE` test is focused and fast; the `transaction=True` test reaches a
genuine COMMIT. Without the second, the suite would be green while never once
exercising the boundary the constraint actually fires at.

### Enforcement beyond the list

- **Idempotency** — a unique `idempotency_key`; a retried command returns the
  entry already posted rather than posting a second one.
- **Atomicity** — the entry, its lines, and its audit event commit together or
  not at all. A half-posted entry is worse than a failed one because it looks
  complete.
- **Gapless numbering** — `JournalNumberSequence` under `select_for_update`,
  scoped per organization and year. `MAX(number)+1` would let two concurrent
  postings claim the same number.
- **Organization consistency** — journal, branch, account, and cost centre must
  all belong to one organization.

## Already delivered by Task 0.5

Invariant 12 depends on the audit foundation, which exists:

- `AuditEvent` is append-only, enforced by a PostgreSQL trigger that raises on
  `UPDATE` and `DELETE`.
- Every event carries actor, correlation ID, branch, before/after state, and
  reason.
- `AuditAction` already defines `POSTED`, `REVERSED`, `POSTING_FAILED`,
  `PERIOD_CLOSED`, and `PERIOD_REOPENED`, so the kernel has the vocabulary it
  needs without extending the enum.

## Already delivered by earlier tasks

- Monetary precision, allocation, and cash rounding — ADR-012,
  `apps/core/money.py`, `apps/core/allocation.py`.
- Quantity precision — ADR-006, `apps/core/quantity.py`.
- Branch scoping and access — ADR-007, `apps/organizations/`.

## Blocked before Task 0.6 can start

- Whether cost centers are organization-wide or branch-scoped (ADR-015 §Open).
- The full chart of accounts beyond the seed (ADR-014 §Open).
- Who may reopen a closed period (ADR-013 §Open).
