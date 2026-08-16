# ADR-013 — Fiscal year and accounting periods

- **Status:** Accepted and **built** by Task 0.6. `FiscalYear`,
  `AccountingPeriod` and `PeriodState` are in `apps/accounting/models.py`;
  the lifecycle lives in `services.close_period` / `reopen_period`; the
  rules are held by `test_posting.py::TestPeriods` and
  `test_hardening.py::TestPeriodOrdering`, and traced as ACC-007. (This line
  read "not yet built" until the Phase 2 gate, long after it was.)
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

## Amendment — reopening authority (approved 2026-08-09, delivered by Task 0.7)

**Reopening is an organization-level accounting permission**,
`accounting.reopen_period`, and the default business role holding it is
**ACCOUNTING_MANAGER** (Chief Accountant). Owner holds it too, as the post that
answers for the ledger as a whole.

It is **not** granted by default to Branch Manager, Branch Accountant,
Cashier, or the warehouse roles. A branch accountant who could both post and
reopen could undo their own close unobserved; that is the separation of duties
the exclusion exists to keep.

**Branch authority never reaches it.** A period spans every branch in the
organization at once, so authority over one branch is authority over a part of
something that has no parts. Organization scope comes from an explicit
`OrganizationMembership` and never from an accumulation of branch memberships
— see ADR-016.

The service checks the **permission and the scope**, never the role name. Role
is an input to permission, not a substitute for it, so a site that renames or
adds roles changes one table and no accounting code.

**Emergency authority.** A Django superuser satisfies both the permission and
the scope, so they may reopen. That is deliberately not a bypass: they reach
the same `reopen_period` service as anyone else, where a non-whitespace reason
is required, reverse-chronological ordering still applies, the actor is
captured, and the audit event is written. Emergency authority changes who may
ask, never what is checked.

Every reopening records actor, timestamp, organization, period, previous
state, new state, and reason. A second `PERMISSION_OVERRIDE` event records the
authority that permitted it, because "did the state change" and "who was
allowed to change it" are different questions and an auditor asks the second.

**Maker-checker is deliberately not an MVP blocker.** One authorized
Accounting Manager is sufficient for Phase 0. The permission sits in front of
an application service rather than inside the kernel, so a second-approver
step can be added later without touching accounting logic.

## Still open

- Whether a second approver should eventually be required for a reopening.
  The architecture allows it; nothing depends on it yet.
