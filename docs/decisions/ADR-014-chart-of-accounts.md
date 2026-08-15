# ADR-014 — Chart of accounts

- **Status:** Accepted and **built** by Task 0.6. `Account` and `AccountClass`
  are in `apps/accounting/models.py`; `seed_chart_of_accounts` seeds the
  chart (77 accounts as of Task 2.15); the code structure and scoping rules
  are held by `test_chart_of_accounts.py` and traced as ACC-013 – ACC-015.
  (This line read "not yet built" until the Phase 2 gate, long after it was.)
- **Date:** 2026-08-08
- **Related:** ADR-012 (monetary precision), ADR-015 (cost centers)

## Decision

A **custom hierarchical restaurant chart of accounts** is the operational COA.
The accounting kernel is **not** hard-coded to the Iraqi Unified Accounting
System.

Statutory reporting is served by **optional mapping**, not by bending the
operational chart:

```
external_accounting_system   e.g. "IQ_UNIFIED"
external_account_code        the code in that system
```

An account may map to zero or one external system code. The mapping never
affects posting.

### Code format

```
C-GG-SS-AAA
```

| Segment | Meaning |
|---|---|
| `C` | Account class |
| `GG` | Group |
| `SS` | Subgroup |
| `AAA` | Posting / detail account |

**Codes are stored as strings, never integers.** `1-01-01-001` is an
identifier, not a number: leading zeros are significant, and arithmetic on an
account code is always a bug.

### Classes

| Class | Meaning |
|---|---|
| 1 | Assets |
| 2 | Liabilities |
| 3 | Equity |
| 4 | Revenue |
| 5 | Cost of Sales / COGS |
| 6 | Operating Expenses |
| 7 | Other Income / Expense |
| 8 | Clearing / Control |
| 9 | Memo / Statistical |

### Seed accounts

```
1-01-01-001  Main Cash
1-01-02-001  Bank
1-02-01-001  Bally Receivable
1-02-01-002  Toters Receivable
1-02-01-003  Talabat Receivable

4-01-01-001  Dine-in Sales
4-01-01-002  Takeaway Sales
4-01-02-001  Delivery App Sales

5-01-01-001  Food COGS
6-01-01-001  Salaries
6-01-02-001  Rent

7-09-01-001  Cash Rounding Gain/Loss
8-01-01-001  Inter-branch Clearing
8-01-02-001  MEM Agency Clearing
```

**`7-09-01-001` Cash Rounding Gain/Loss must exist in the seeded chart even
though `CASH_ROUNDING_ENABLED = False`** (ADR-012). Enabling cash rounding
later must fail validation if the configured rounding account is missing or
cannot accept postings — a settlement path that discovers a missing account at
runtime would either crash mid-transaction or silently bury the residual.

### Posting rules

- **Only leaf accounts accept journal lines.** A parent or group account is a
  reporting rollup; posting to one makes its children's balances no longer sum
  to it, and no report can then be trusted.
- An account referenced by a posted journal cannot be destructively deleted.
  Deactivate instead.

## Alternatives considered

- **Adopt the Iraqi Unified Accounting System as the operational chart.**
  Statutory alignment for free, but its structure is not built for restaurant
  cost control — no natural place for channel revenue, app commissions, or
  recipe-level COGS. Mapping gives compliance without distorting operations.
- **Integer account codes.** Smaller, and silently destroys leading zeros the
  first time one is parsed.
- **Allowing postings to group accounts** "just for corrections". This is how
  a trial balance stops reconciling to its own detail.

## Amendment — hierarchy exclusivity (approved 2026-08-08)

Codes carry their level, and the four levels are:

```
1            class
1-01         group
1-01-01      subgroup
1-01-01-001  posting / detail account
```

Two invariants make the hierarchy trustworthy rather than decorative:

- **An account that has ever received posted lines must never become a
  parent.** Adding a child beneath it would turn a posting account into a
  rollup, and its own historic lines would then sit at a level that no longer
  accepts them — the hierarchy would stop summing correctly from that day
  backwards.
- **An account with children must never accept postings.**

Both are *structural* first: only a four-segment code is postable and no valid
code extends one, so neither state is reachable through the code scheme.
`validate_parent_has_no_posting_history` and the children check in
`validate_accounts_are_postable` are the second lock, so a future change to the
code scheme cannot quietly open the hole.

Archived codes stay reserved permanently, as already implemented.

## Consequences

- The seed is deterministic reference data, like the units seed, so reports are
  reproducible.
- `is_leaf` (or equivalent) must be enforced at the database level, not only in
  the posting service.
- Adding a child to an account that already has postings is a migration
  problem, because that account would stop being a leaf. Needs a guard.

## Settled since (was "Open")

All three were answered by Task 0.6 and its successors; the section stayed
open long after the code closed it, which is how a reader could conclude the
chart had never been designed.

- **The full account list beyond the seed.** `seed_chart_of_accounts` is the
  list, and it grows with the tasks that need roles: 46 accounts at Phase 0,
  74 by Task 2.13, 77 once Task 2.15 added supplier advances. The count is
  asserted, so a chart change cannot pass unnoticed.
- **Per organization or global codes?** **Per organization**, enforced by
  `account_code_unique_per_organization`. Two organizations may use the same
  code for different accounts, and an archived code stays reserved within its
  own (`test_chart_of_accounts.py::TestScoping`, ACC-014).
- **One-to-one external mapping, or several statutory systems?** **One**, and
  it is complete or absent — never half-filled — enforced by
  `account_external_mapping_is_complete_or_absent`. It affects no posting
  (`test_chart_of_accounts.py::TestExternalMapping`, ACC-015).
