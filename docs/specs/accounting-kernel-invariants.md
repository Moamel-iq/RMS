# Accounting kernel — enforced invariants (Task 0.6)

The approved checklist Task 0.6 must satisfy. Each line is a test, not a
guideline. Nothing in this list is optional and none of it may be relaxed to
make a suite pass.

| # | Invariant | Enforced where | ADR |
|---|---|---|---|
| 1 | Journal entry debits equal credits, compared on **stored 3-decimal values** | Posting service **and** database constraint | ADR-012 |
| 2 | No float anywhere in an accounting calculation | `apps/core/money.py` guards | ADR-012 |
| 3 | No posting to a non-leaf account | Posting service + DB | ADR-014 |
| 4 | No posting to a `CLOSED` period | Posting service | ADR-013 |
| 5 | Every journal line belongs to exactly one branch | Non-null FK | ADR-015 |
| 6 | Cost centers required per the account's `requires_cost_center` policy | Posting service | ADR-015 |
| 7 | All allocation is deterministic | `apps/core/allocation.py` | ADR-012 |
| 8 | Document total equals the sum of its stored posted lines | Posting service | ADR-012 |
| 9 | UI rounding never affects ledger values | Renderers return `str` | ADR-012 |
| 10 | Accounts, cost centers, and branches used by posted journals cannot be destructively deleted | `on_delete=PROTECT` | ADR-014, ADR-015 |
| 11 | Posted journals are immutable; corrections are reversals | DB trigger + service | ADR-004 |
| 12 | Posting, reversal, and period reopening are audit logged | `apps.core` audit foundation | Task 0.5 |

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
