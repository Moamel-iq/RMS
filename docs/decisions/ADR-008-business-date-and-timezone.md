# ADR-008 — Business date and timezone

- **Status:** Accepted (schema); the per-branch cutoff **value** is open
- **Date:** 2026-08-08
- **Related:** `docs/architecture/architecture-charter.md` §10, ADR-007

## Context

Khan Mandi trades past midnight. A sale rung at 01:30 belongs to the previous
operating day, not to the new calendar day. The charter is emphatic that the
business date must not be derived as `date(timestamp)` and that the rule is
configurable per branch.

## Decision

**Store both.** Every transactional record will carry a timezone-aware
timestamp *and* a separate business `DateField`. Neither is derived from the
other at read time.

**Timestamps are UTC.** `USE_TZ = True`, `TIME_ZONE = "Asia/Baghdad"` for
display. Storage is UTC everywhere.

**The rule lives on the branch.** `Branch.timezone` and
`Branch.business_day_start_time` are fields, set per branch. A timestamp `t`
belongs to business date `D` when, in the branch's own timezone:

```
D 00:00 + business_day_start_time  <=  t  <  (D+1) 00:00 + business_day_start_time
```

A single start time defines a 24-hour window. This is why the field is a
*start*, not a pair of open/close times: a business day that both starts at
09:00 and closes at 03:00 would leave 03:00–09:00 belonging to no day at all,
and every transaction must map to exactly one.

**One implementation, later.** `business_date_for(timestamp, branch)` is a
single tested domain service delivered in its own task. No module computes it
inline. Task 0.3 ships only the fields it will read.

## Alternatives considered

- **`date(timestamp)`** — rejected by the charter. Splits a single trading
  night across two reporting days.
- **A global cutoff in settings** — cheaper, but the charter requires
  per-branch configuration, and branches keep different hours.
- **Storing an explicit open and close time** — creates gaps and overlaps.
  A 24-hour window anchored on one start time cannot.

## Consequences

- `Branch.business_day_start_time` is non-null. There is no "unset" state that
  a later calculation could silently misread.
- `Branch.timezone` is validated against the IANA database at write time. An
  unknown zone would make every business date on that branch wrong.
- Changing a branch's cutoff after transactions exist retroactively reassigns
  their business dates. This is a controlled operation and is **not**
  implemented; the field is editable today only because no ledger exists yet.
  Before Phase 1 posts anything, this needs an audited change procedure.

## Open — needs a decision from the business

The **value** of the cutoff is not decided. The charter offers "starts at
09:00, closes around 03:00" as an illustration, not as policy. Required before
any transactional module ships:

1. The actual operating-day start time for the Al-Bunook branch.
2. Whether every branch shares one cutoff or each is set independently.
3. Whether attendance and payroll use the same business date as sales, or the
   calendar date.

Until these are answered, no default is written into a migration.
