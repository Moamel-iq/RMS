# Architecture Decision Records

Each ADR states: status, context, decision, alternatives considered,
consequences, date, and related requirements.

## Accepted

| ADR | Title |
|---|---|
| [ADR-001](ADR-001-django-5-2-lts-python-3-14.md) | Django 5.2 LTS on Python 3.14 |
| [ADR-002](ADR-002-postgresql-18.md) | PostgreSQL 18 as the only database |
| [ADR-006](ADR-006-decimal-and-rounding-policy.md) | Decimal and rounding policy — quantities |
| [ADR-012](ADR-012-monetary-precision-and-allocation.md) | Monetary precision, allocation, and cash rounding (IQD) |
| [ADR-013](ADR-013-fiscal-year-and-accounting-periods.md) | Fiscal year and accounting periods — *implemented by Task 0.6* |
| [ADR-014](ADR-014-chart-of-accounts.md) | Chart of accounts — *implemented by Task 0.6* |
| [ADR-015](ADR-015-cost-centers-and-branch-dimension.md) | Cost centers and the branch dimension — *implemented by Task 0.6* |
| [ADR-007](ADR-007-organization-and-branch-boundaries.md) | Organization and branch boundaries |
| [ADR-008](ADR-008-business-date-and-timezone.md) | Business date and timezone (schema only — cutoff value open) |
| [ADR-010](ADR-010-windows-native-development-environment.md) | Windows-native development environment and pip-tools |
| [ADR-011](ADR-011-htmx-frontend.md) | Django templates + htmx for the frontend |

## Reserved, not yet written

These numbers are reserved by the installation plan. Each needs a business
decision from the product owner before it can be written, and several block
Phase 0 tasks.

| ADR | Title | Blocks |
|---|---|---|
| ADR-003 | Service / selector architecture | — (pattern already in CLAUDE.md; formalise before Task 0.6) |
| ADR-004 | Append-only ledgers | Task 0.6 |
| ADR-005 | Moving weighted-average costing | Phase 1 |
| ADR-009 | Arabic, RTL, and PDF strategy | Phase 7 reporting |

## Open questions that must be answered before the ADRs above can be written

Sourced from `docs/plans/phase-0-claude-code-prompts.md`. None of these have
documented answers yet:

1. **Cost center scope** — organization-wide or branch-scoped? The charter
   places cost centers beneath Branch, but Delivery and Administration
   plausibly span branches. **Blocks Task 0.6** (ADR-015 §Open).
2. **The full chart of accounts** beyond the seed, and whether account codes
   are unique per organization or globally (ADR-014 §Open).
3. **Who may reopen a closed period**, and whether a second approver is
   required (ADR-013 §Open).
4. **Business day cutoff** — the actual start time for Al-Bunook; whether all
   branches share one cutoff; whether attendance and payroll use the same
   business date as sales. The *schema* is settled (ADR-008); only the values
   are open, and no default is written into any migration.
5. **Inventory valuation scope** — confirm Organization + Branch + Warehouse +
   Item. Phase 1.
6. Whether one branch may hold **multiple warehouses** at go-live. Phase 1.
7. **Role list** — the roles in `apps/organizations/models.py::Role` are taken
   from the charter's separation-of-duties examples, not from an SRS.
   Approval thresholds are not enforced yet.

## Settled

- **Quantity precision and rounding** — ADR-006.
- **Conversion factor precision** — 12 places, confirmed; not to be reduced.
- **Monetary precision, allocation, and cash rounding** — ADR-012.
  Nearest-250 rounding is OFF and must stay off for all accounting values.
- **Fiscal year and period granularity** — ADR-013. January start, monthly,
  no period 13.
- **Chart of accounts structure and code format** — ADR-014. Custom
  restaurant chart with optional statutory mapping.
- **Cost center policy** — ADR-015. Branch required on every line, cost
  center driven by `Account.requires_cost_center`.
