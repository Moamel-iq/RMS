# ADR-012 — Monetary precision, allocation, and cash rounding (IQD)

- **Status:** Accepted
- **Date:** 2026-08-08
- **Related:** ADR-006 (quantities), ADR-004 (append-only ledgers)
- **Implements:** `apps/core/money.py`, `apps/core/allocation.py`

## Context

ADR-006 settled quantity precision and deliberately left money undecided,
because a monetary policy needs answers a quantity policy does not: what a
posted line stores, what a printed document shows, how a shared amount is
split without losing a dinar, and what happens to a residual.

## Decision

### Precision

| Constant | Value | Applies to |
|---|---|---|
| `MONEY_PLACES` | 3 | Posted accounting and document line amounts |
| `MONEY_DISPLAY_PLACES` | 0 | IQD shown in the UI or printed |
| `UNIT_PRICE_PLACES` | 6 | Unit costs and unit prices |
| `RATE_PLACES` | 6 | Commission, discount, and allocation rates |
| `MONEY_ROUNDING` | `ROUND_HALF_UP` | All of the above |

Money shares **no constants and no functions** with quantities. The two live
in separate modules and every public name is prefixed accordingly, so an
amount cannot inherit quantity rounding by accident.

Ties round away from zero, as with quantities, and for the same reason:
corrections happen by reversal, so a credit of `-1250.0005` must mirror a
debit of `1250.0005` exactly.

### Calculation and posting

1. Calculate at full Decimal precision. Never round mid-calculation.
2. Quantize to 0.001 IQD **once**, at the moment the amount becomes a posted
   accounting or document line.
3. **A document total is the sum of its posted lines.** It is never rounded
   independently. Rounding the total separately from the lines is precisely
   how `SUM(lines) != total` arises, and that discrepancy is invisible until
   someone reconciles a statement months later.

Display rounding to whole dinars is a presentation transform only. A
display-rounded value must never be stored, summed, or fed back into a
calculation.

### Residual allocation

Proportional splits — discounts, application commissions, shared expenses,
document-level charges, cost allocations — go through
`allocate_proportionally`, which guarantees:

```
sum(parts) == quantize_money(total)      exactly, for every input
```

Method is **largest remainder (Hamilton)**: compute exact shares at high
precision, floor each to a whole quantum of 0.001 IQD, then hand the residual
out one quantum at a time to the largest fractional remainders. **Ties break
on line order**, so the result is reproducible. Callers must pass lines in a
stable order — line sequence or primary key.

Exactness is structural rather than approximate: the residual is derived by
subtracting the floors from the target, so whatever rounding happened while
computing shares cannot change the sum.

A rate is applied to the total and the product allocated once, never applied
line by line. Rating each line separately rounds each independently, and those
roundings do not add back up to the rate applied to the total.

### Residuals that are not allocation

A residual arising from an external settlement or from cash rounding is a real
gain or loss, not a distribution problem. It posts to an explicit **cash
rounding gain/loss account**. It is never buried in revenue, COGS, inventory,
or whichever line happens to be last.

### Nearest-250 rounding

**Off.** `CASH_ROUNDING_ENABLED = False`.

It must never apply to sales values, purchase invoices, supplier balances,
application receivables, application commissions, payroll, inventory
valuation, recipe costing, COGS, journal entries, taxes, or discounts.

The architecture supports enabling it later as a configurable **cash
settlement** policy. `apply_cash_settlement_rounding` already returns
`(rounded, adjustment)` so that callers are written against the final shape
today. When enabled it must:

- apply only to the final amount physically payable in cash;
- run only after every other document calculation is complete;
- leave the underlying sales, tax, and COGS values untouched;
- post the difference explicitly to the cash rounding gain/loss account.

## Alternatives considered

- **Two decimal places**, as for a currency with cents. Rejected: IQD has no
  circulating subdivision, and the extra place absorbs allocation residuals
  without them being visible to an operator reading whole dinars.
- **Rounding the document total independently.** Simpler, and it produces
  totals that do not equal the sum of their own lines.
- **Distributing the residual to the largest or the last line.** Deterministic
  but biased: the same line absorbs every rounding difference forever.
- **Banker's rounding (`ROUND_HALF_EVEN`).** Unbiased over many roundings and
  common in finance, but it breaks the reversal symmetry above and surprises
  an operator checking a figure by hand.

## Consequences

- Posted amounts carry a third decimal that is never displayed. An operator
  seeing `1,250` may be looking at `1250.001`. Reconciliation reports must
  compare stored values, not displayed ones.
- `money_for_display` exists precisely so nobody reaches for `round()`.
- Every allocation path must pass lines in a stable order. An unordered
  queryset would make the residual land on a different line between runs; no
  guard enforces this yet, and it is the most likely way to misuse the module.
- The cash rounding account does not exist yet. It must be added to the chart
  of accounts before the policy is ever enabled.

## Open

- The **cash rounding gain/loss account code**, once the chart of accounts is
  decided.
- Whether cash settlement rounding is per-organization or per-branch when it
  is eventually enabled. `CASH_ROUNDING_ENABLED` is a module constant today
  and becomes configuration at that point.
