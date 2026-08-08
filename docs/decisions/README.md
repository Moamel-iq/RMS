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

1. **Fiscal year start month**, and period granularity (monthly or custom).
   **Blocks Task 0.6.**
2. **Chart of accounts** — the Iraqi unified accounting system, or a custom
   restaurant chart? Account code format? **Blocks Task 0.6.** Must include a
   **cash rounding gain/loss account** before cash settlement rounding is ever
   enabled (ADR-012).
3. Whether **cost centers** are required on every journal line or optional.
   **Blocks Task 0.6.**
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
