# Architecture Decision Records

Each ADR states: status, context, decision, alternatives considered,
consequences, date, and related requirements.

## Accepted

| ADR | Title |
|---|---|
| [ADR-001](ADR-001-django-5-2-lts-python-3-14.md) | Django 5.2 LTS on Python 3.14 |
| [ADR-002](ADR-002-postgresql-18.md) | PostgreSQL 18 as the only database |
| [ADR-006](ADR-006-decimal-and-rounding-policy.md) | Decimal and rounding policy — **quantities only; money still open** |
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

1. **IQD rounding** — decimal places stored, decimal places displayed,
   rounding mode, whether line values or only document totals are rounded,
   how residual differences are allocated, and whether any amount rounds to
   the nearest 250 IQD. **Blocks Task 0.6 (accounting kernel).**
   Quantity precision is settled; money deliberately shares none of it.
2. **Conversion factor precision** — `FACTOR_PLACES = 12` is inferred, not
   decided. Twelve stores one ounce exactly (0.028349523125 kg); everything
   metric needs at most six. Confirm or reduce. See ADR-006 §Open.
3. **Fiscal year start month**, and period granularity (monthly or custom).
4. **Chart of accounts** — the Iraqi unified accounting system, or a custom
   restaurant chart? Account code format?
5. **Inventory valuation scope** — confirm Organization + Branch + Warehouse +
   Item.
6. Whether one branch may hold **multiple warehouses** at go-live.
7. Whether **cost centers** are required on every journal line or optional.
8. **Business day cutoff** — the actual start time for Al-Bunook; whether all
   branches share one cutoff; whether attendance and payroll use the same
   business date as sales. The *schema* is settled (ADR-008); only the values
   are open, and no default is written into any migration.
9. **Role list** — the roles in `apps/organizations/models.py::Role` are taken
   from the charter's separation-of-duties examples, not from an SRS.
   Approval thresholds are not enforced yet.
