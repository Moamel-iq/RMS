# ADR-013 — Fiscal year and accounting periods

- **Status:** Accepted. **Implemented by Task 0.6**, not yet built.
- **Date:** 2026-08-08
- **Related:** ADR-008 (business date), ADR-012 (monetary precision)

## Decision

**Fiscal year.** `FISCAL_YEAR_START_MONTH = 1`. The default fiscal year runs
1 January to 31 December.

Stored as an **organization-level setting**, not a global constant, so a second
organization can differ. Once accounting postings exist it must not be casually
editable: changing it re-buckets every posted entry. Editing requires an
explicit controlled migration process, and the field must be guarded once the
ledger is live.

**Granularity: MONTHLY.** Twelve normal accounting periods per fiscal year.
Journals keep their real daily posting dates; the period is a bucket over
those dates, never a replacement for them.

**No Period 13.** Year-end adjustments post on the fiscal year-end date and
carry `is_adjustment = True`. A thirteenth period would be a second calendar
that every report then has to decide whether to include.

**Period states.**

| State | Meaning |
|---|---|
| `OPEN` | Normal posting permitted |
| `SOFT_CLOSED` | Routine posting stops; authorized corrections still allowed |
| `CLOSED` | No posting at all |

Nothing may post into a `CLOSED` period except through an explicit authorized
**reopening workflow**, and every reopening is audit logged — actor, reason,
timestamp, and the period affected.

## Alternatives considered

- **A separate Period 13** for year-end adjustments. Common in older ledgers,
  and it forces every report to answer "does this include period 13?". A dated
  adjustment flag carries the same information without the second calendar.
- **A single OPEN/CLOSED pair.** Simpler, but leaves no state for "the month is
  done for operations, the accountant is still working".
- **A global fiscal year setting.** Cheaper, and wrong the moment a second
  organization is onboarded.

## Consequences

- Every posting service must resolve the period from the accounting date and
  refuse a non-`OPEN` period, except on the authorized correction path.
- Reopening is a privileged action needing its own permission and audit event.
  `apps.core` (Task 0.5) supplies the audit event type.
- The business date (ADR-008) and the accounting date are distinct. A sale at
  01:30 belongs to the previous business day, and the period is derived from
  the accounting date, not from the timestamp.

## Amendment — period lifecycle ordering (approved 2026-08-08)

**Closing is chronological.** A period cannot become `CLOSED` while an earlier
period in the same fiscal year is not `CLOSED`. Sealing February while January
still accepts entries would let January's closing figures — and every balance
carried forward from them — change afterwards.

**Reopening is reverse-chronological.** A `CLOSED` period cannot be reopened
while a later period in the same year is still `CLOSED`, for the same reason
in the other direction.

```
Close:   Jan -> Feb -> Mar -> ...
Reopen:  ... -> Mar -> Feb -> Jan
```

Soft close is deliberately **not** order-constrained: it is reversible and
carries no figures forward, so sealing March before February is unusual rather
than unsound.

**Fiscal-year closure is derived, never stored.** `FiscalYear.is_closed` is a
property: true when every period in the year is `CLOSED`. A stored flag would
be a second source of truth that could disagree with the periods postings are
actually checked against.

**Soft-close authorization.** `SOFT_CLOSED` blocks routine posting but permits
specifically-authorized adjustments and reversals. Those permissions arrive in
Task 0.7 (`accounting.post_soft_closed_adjustment`,
`accounting.reverse_in_soft_closed_period`), each requiring a non-empty reason
and a recorded actor. Until then the capability exists and nothing restricts
who may use it.

## Open

- Who may reopen a period — which role, and whether a second approver is
  required.
