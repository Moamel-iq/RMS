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
