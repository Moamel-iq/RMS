# ADR-015 — Cost centers and the branch dimension

- **Status:** Accepted and **built** by Task 0.6. `CostCenter` is in
  `apps/accounting/models.py`, scoped to the organization; the
  account-driven requirement policy is held by
  `test_posting.py::TestCostCenterPolicy` and traced as ACC-004. (This line
  read "not yet built" until the Phase 2 gate, long after it was.)
- **Date:** 2026-08-08
- **Related:** ADR-007 (organization and branch boundaries), ADR-014 (COA)

## Decision

**Branch and cost center are separate concepts.** Branch is *where the money
moved*; cost center is *which activity consumed it*.

| Dimension | On a journal line |
|---|---|
| `branch` | **Required**, always |
| `cost_center` | **Optional** at the schema level |

Cost centers are **not** required on every accounting line. Requiring one
everywhere forces meaningless values onto balance-sheet control accounts —
"which cost center does the bank balance belong to?" has no answer, and
whatever gets entered corrupts managerial analysis.

Instead the requirement lives on the account:

```
Account.requires_cost_center = True | False
```

Default policy:

| Account class | `requires_cost_center` |
|---|---|
| Revenue | **True** |
| COGS | **True** |
| Operating Expenses | **True** |
| Cash | False |
| Bank | False |
| Receivables | False |
| Payables | False |
| Equity | False |
| Clearing / Control | False |

The posting service enforces the account's policy: a line hitting an account
with `requires_cost_center = True` and no cost center is refused.

**Cost centers are organization master data, not kernel constants.** The
initial structure supports at least Kitchen, Hall, Warehouse, Delivery,
Administration, and HR — but those names are **seeded data**, never hard-coded
into the accounting kernel. A branch that adds a bakery must not require a code
change.

## Alternatives considered

- **Cost center required on every line.** Uniform and simple to validate, and
  it produces a P&L where every balance-sheet account carries a fictional
  cost center. Rejected.
- **Cost center optional everywhere, enforced only by report convention.**
  Nothing stops a revenue line posting without one, and the gap is discovered
  at month-end when the analysis is already wrong.
- **Cost center as a second branch-like hierarchy.** Conflates location with
  activity; a delivery cost center exists at every branch.

## Consequences

- Both dimensions are validated in the posting service, and `branch` is
  non-null at the database level.
- Managerial P&L by activity is possible without polluting the balance sheet.
- A cost center referenced by a posted journal cannot be destructively
  deleted. Deactivate instead.
- Changing `requires_cost_center` on an account with history does not
  retroactively validate old lines. Any tightening needs a backfill decision.

## Settled since (was "Open")

- **Organization-wide or branch-scoped?** **Organization-wide**, settled
  before Task 0.6 modelled it and recorded at
  `docs/specs/accounting-kernel-invariants.md`. Delivery and Administration
  span branches, and the branch dimension is already carried on the journal
  line, so scoping the cost centre to a branch as well would record the same
  fact twice and let the two disagree
  (`test_chart_of_accounts.py::test_a_cost_center_belongs_to_the_organization_not_a_branch`).
  This entry still read "**Must be settled before Task 0.6 models it**" at
  the Phase 2 gate, years of tasks after it was.
- **May a specific account require a cost centre its class would not?**
  **Yes.** `requires_cost_center` is a column on `Account`, defaulted from
  the class and overridable per account; the posting service reads the
  account, never the class (`test_posting.py::TestCostCenterPolicy`,
  ACC-004).
