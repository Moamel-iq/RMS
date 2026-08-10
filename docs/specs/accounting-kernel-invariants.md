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
| A domain may veto a close it would strand work inside | `_run_period_close_guards` | `test_waste_counts_adjustments.py::test_an_active_count_blocks_closing_its_period` |
| Fiscal-year closure is **derived**, never stored | `FiscalYear.is_closed` property | `TestFiscalYearClosureIsDerived` |
| A second reversal reports `already_reversed`, not `not_posted` | `reverse_entry` checks the relationship first | `TestReversalErrorAccuracy` |
| The deferred balance trigger fires at a real COMMIT | `test_commit_boundary.py` (`transaction=True`) | `test_an_unbalancing_line_is_refused_at_commit` |

**Closing a period asks the domains first (Task 1.6).** `register_period_close_guard`
is the same shape as `register_mapping_guard`, and exists for the same reason:
accounting owns the period lifecycle and must not learn what a stock count is,
while inventory must not reach into the period state machine. Inventory
registers one veto at app-ready — a period whose dates cover an active physical
count refuses to soft-close or close with `active_inventory_count`, because a
count that freezes a warehouse on the 30th and finds the month shut on the 1st
can neither post nor usefully be cancelled.

The guards run **inside the transaction, under the period's row lock**, and
`apps.inventory.counts.start_count` takes that same row lock before checking
the period. Without that pairing both can commit: neither transaction sees the
other's uncommitted work under READ COMMITTED, so the close finds no active
count and the count finds an open period. See ADR-021 §10.

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
`CLOSED` — nothing posts or reverses. **Task 0.7 supplied the authorization**:
`accounting.post_soft_closed_adjustment` and
`accounting.reverse_in_soft_closed_period`, each held at organization scope,
each requiring a non-empty reason, and each recording a `PERMISSION_OVERRIDE`
audit event separate from the posting itself.

**Both balance tests are kept on purpose.** The `SET CONSTRAINTS ALL
IMMEDIATE` test is focused and fast; the `transaction=True` test reaches a
genuine COMMIT. Without the second, the suite would be green while never once
exercising the boundary the constraint actually fires at.

### Task 0.7 additions

| Rule | Where | Test |
|---|---|---|
| A posted entry is immutable on **every** column, not a remembered subset | `accounting_posted_entry_is_immutable`, migration `0005` | `test_a_posted_narration_cannot_be_rewritten`, `test_no_other_posted_column_can_be_rewritten_either` |
| A draft promoted to POSTED is balanced and has ≥2 lines | Constraint trigger `accounting_journalentry_balance_on_post`, migration `0004` | `test_an_unbalanced_draft_is_refused_at_posting_not_at_creation` |
| A draft holds no entry number; numbering stays gapless | Partial unique + `journal_entry_numbered_once_posted` | `test_create_amend_post` |
| One economic event, one journal, per organization | `journal_entry_source_event_unique_per_organization` | `TestTheGuaranteeSurvivesACommit` |
| A source identity is complete or absent | `journal_entry_source_identity_complete_or_absent`, `validate_source_identity` | `TestIdentityIsCompleteOrAbsent` |
| `source_event` is a closed enum | `journal_entry_source_event_is_known` + `TextChoices` | `TestTheEnumIsClosed` |
| Source identity is immutable once posted | Immutability trigger | `TestSourceIdentityIsImmutable` |
| Posting into `SOFT_CLOSED` needs organization authority and a reason | `commands._require_soft_close_override` | `TestSoftClosedPeriodOverHttp` |
| Reopening needs `accounting.reopen_period` at organization scope | `commands.reopen_accounting_period` | `test_11_...`, `test_12_...` |
| A submitted id cannot widen access | `organizations/authorization.py` | `apps/accounting/tests/test_security.py` |

**Two defects were found by these tests, not by review.**

The first: the immutability trigger from 0002 permitted its two legitimate
transitions by listing the columns that must *not* change. That is a blocklist,
and it was missing `narration`, `document_date`, `is_adjustment`, `posted_at`,
`posted_by`, and `posting_rule_version` — all editable on a posted entry, with
no error, no history row, and no audit event. Migration `0005` inverts the
test: each permitted transition now builds the row it expects and compares
whole rows, so a column added later is covered without anyone remembering to
add it.

The second: organization-wide authority granted no branch access, so an
Accounting Manager could reopen a period covering every branch and post an
adjustment into none of them. `accessible_branches` now reaches branches
through an organization membership as well as a branch one. The containment
runs one way only — see ADR-016.

### Task 0.8 additions (Phase 0 exit gate)

| Rule | Where | Test |
|---|---|---|
| An idempotency key is unique per **organization** | `journal_entry_idempotency_key_unique_per_organization` | `test_idempotency.py::TestKeysAreScopedToTheOrganization` |
| A replay is verified against a fingerprint of the request | `services._replay`, `_idempotency_fingerprint` | `::TestSameKeyDifferentRequest` |
| A key never returns another organization's journal | org-scoped lookup and selector | `::test_a_key_cannot_be_used_to_discover_another_organizations_journal` |
| Out of scope answers 404; in scope without authority answers 403 | `OutOfScope`, `PermissionMissing` | `test_security.py`, `test_api.py` |
| A seed survives a console that cannot encode Arabic | `apps.core.console.SeedCommand` | `tests/test_phase_0_exit.py` |
| The foundations cooperate end to end | — | `tests/test_phase_0_exit.py::TestTheFoundationsCooperate` |

**Two more defects found by tests rather than review.**

`idempotency_key` was globally unique and matched on the key alone. Both
halves were wrong, and together they were a cross-tenant read: posting into
organization B with a key organization A had already used returned *A's
journal*. Keys are frequently predictable — upstream modules build them from
document numbers — so this was reachable by guessing a string. It is now
unique per organization, looked up per organization, and matched against a
fingerprint of the request.

`seed_units` printed Arabic to stdout inside an `@transaction.atomic` command.
On a Windows cp1252 console that raises `UnicodeEncodeError`, which rolled
back every unit already written: a fresh install ended with a traceback and an
empty table. No test caught it because the development database already held
units and pytest captures stdout as UTF-8; it took a genuinely empty database
on a real console.

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

## Previously blocked, now resolved

- Whether cost centers are organization-wide or branch-scoped — **organization**
  (ADR-015), settled before Task 0.6.
- The full chart of accounts beyond the seed — **46 accounts seeded** (ADR-014).
- Who may reopen a closed period — **ACCOUNTING_MANAGER at organization scope**
  (ADR-013 amendment, delivered by Task 0.7).
