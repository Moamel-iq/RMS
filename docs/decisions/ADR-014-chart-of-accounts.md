# ADR-014 — Chart of accounts

- **Status:** Accepted. **Implemented by Task 0.6**, not yet built.
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

## Consequences

- The seed is deterministic reference data, like the units seed, so reports are
  reproducible.
- `is_leaf` (or equivalent) must be enforced at the database level, not only in
  the posting service.
- Adding a child to an account that already has postings is a migration
  problem, because that account would stop being a leaf. Needs a guard.

## Open

- The full account list beyond the seed above.
- Whether account codes are unique per organization or globally.
- Whether the external mapping is one-to-one or allows several statutory
  systems per account.
