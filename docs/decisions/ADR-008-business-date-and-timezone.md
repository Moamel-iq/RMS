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

## Amendment applied at Task 1.4 (2026-08-10) — what the business date *governs*

The schema was settled here; what nobody had written down was which of the two
timestamps a posting is actually judged by. The inventory kernel was validating
the accounting period against `effective_at.date()` — the physical calendar
date — while the document layer validated it against the business date. Both,
in other words. That is now fixed, and the rule is:

    effective_at    the physical moment the event happened
    business_date   the authoritative operational and accounting date

**The business date governs everything that dates a document**: accounting-period
validation, inventory-period validation, account-mapping effective-date
selection, item-conversion effective-date selection, document numbering by
business year, daily operational reporting, and the accounting date of the
journal. `effective_at` is retained beside it, always, and substituted for it
nowhere.

**An event requires exactly one open period — its own.** Under an 03:00 cutoff,
a receipt at 01:30 on 1 August belongs to the 31 July operating day. July must
be open. August must **not** additionally be required, and is not: demanding
the calendar month as well would refuse a legitimate late-night posting the
moment the new month was closed ahead of it, for an event that happened on one
business day and belongs in one set of figures.

### Snapshots: a committed date does not move

The Consequences section above notes that changing a branch's cutoff
retroactively reassigns business dates, and that this needs a controlled
procedure. Task 1.4 supplies the half that protects documents already in
flight: **anything that has committed to a business date also stores the
timezone and cutoff it was derived with**, and re-derives only from those.

| Document state | Behaviour |
|---|---|
| Opening stock, DRAFT | Business date is a preview, recalculated as the cutoff field changes |
| Opening stock, SUBMITTED | Date **and** snapshot fixed; posting replays the snapshot |
| Opening stock, returned to draft | Snapshot released; resubmission derives afresh |
| Operational documents (post directly from draft) | Date and snapshot fixed at posting |

So an approver who reads "31 July" on a submitted document posts 31 July, even
if the branch cutoff was changed in between. Moving a submitted document to a
different period is then a deliberate act — return it to draft and resubmit —
rather than something that happens behind the approver's back.

`apps/organizations/business_dates.py` is the single implementation:
`resolve_business_day` produces a date with its snapshot, and
`business_date_from_snapshot` replays one. Deriving a business date any other
way, and `date(timestamp)` above all, remains a defect.
