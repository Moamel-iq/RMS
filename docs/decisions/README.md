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
| [ADR-016](ADR-016-permission-and-scope-model.md) | Permission and scope model — *implemented by Task 0.7* |
| [ADR-017](ADR-017-source-identity-and-idempotency.md) | Source identity and idempotency — *implemented by Task 0.7* |
| [ADR-018](ADR-018-inventory-valuation-and-the-stock-ledger.md) | Inventory valuation and the stock ledger — *ledger delivered by Task 1.2* |
| [ADR-019](ADR-019-account-roles-and-domain-owned-posting-mappings.md) | Account roles and domain-owned posting mappings — *implemented by Task 1.3* |
| [ADR-020](ADR-020-transfer-ownership-in-transit-valuation-and-cross-branch-accounting.md) | Transfer ownership, in-transit valuation and cross-branch accounting — *implemented by Task 1.5* |
| [ADR-021](ADR-021-physical-count-cutoff-warehouse-freeze-and-count-valuation.md) | Physical count cutoff, warehouse freeze and count valuation — *implemented by Task 1.6* |
| [ADR-022](ADR-022-supplier-return-valuation-and-purchase-variance.md) | Supplier return valuation and purchase variance treatment — *implemented by Tasks 2.12–2.14* |
| [ADR-023](ADR-023-grni-clearing-and-three-way-matching.md) | GRNI clearing and three-way matching allocations — *implemented by Tasks 2.11–2.12* |
| [ADR-024](ADR-024-recipe-versioning-and-the-effective-dated-cost-basis.md) | Recipe structure, versioning and the effective-dated cost basis — *lifecycle, evidence, dating and immutability implemented by Task 3.2A; the nested-recipe graph by Task 3.2B; the cost basis, snapshots and the reproducible ledger cutoff by Task 3.3. Its whole original scope is now built* |
| [ADR-025](ADR-025-production-posting-value-conservation-and-reversal.md) | Production posting, value conservation and reversal — *implemented by Task 3.5* |
| [ADR-026](ADR-026-kitchen-custody-consumption-and-usage-variance.md) | Kitchen custody, consumption and the boundary of usage variance — *implemented by Task 3.8; its deferred sales-based source supplied by Phase 4* |
| [ADR-027](ADR-027-sales-recognition-and-application-receivables.md) | Sales recognition, application receivables and the cashier boundary — *implemented by Phase 4* |
| [ADR-028](ADR-028-sales-discounts-commissions-and-settlements.md) | Sales discounts, commissions and settlements — *implemented by Phase 4* |
| [ADR-029](ADR-029-accounting-operations-and-manual-journals.md) | Accounting operations, manual journals and the read-only subledger workspaces — *implemented by Phase 5 checkpoints 1–4* |
| [ADR-030](ADR-030-cash-bank-expenses-and-deferrals.md) | Cash, bank, expense vouchers, accruals and prepayments — *implemented by Phase 5 checkpoints 3, 5, 6* |
| [ADR-031](ADR-031-financial-statements-and-year-end-close.md) | Financial-statement mapping, current-year earnings and year-end close — *implemented by Phase 5 checkpoints 7–9* |
| [ADR-032](ADR-032-supplier-credit-term-snapshots.md) | Effective-dated supplier credit terms and immutable invoice approval snapshots — *implemented by Procurement completion capability 3* |

Four of these were missing from this table while their files read
**Accepted** and their behaviour shipped, which is how the index came to
disagree with the decisions it indexes. Found at the Phase 2 gate.

Four more had drifted the same way by the Phase 5 audit — ADR-025 and ADR-026
were still listed below as "proposed, not yet written" while both files read
**Accepted** and both behaviours had shipped, and ADR-027 / ADR-028 had never
been indexed at all. All four are in the table above now, and the rows that
claimed otherwise are gone.

## Proposed by Phase 3, not yet written

Nothing. Every decision this section registered has been written and accepted:
ADR-024 by Task 3.2A (with its remaining halves named inside it and delivered
by 3.2B and 3.3), ADR-025 by Task 3.5, and ADR-026 by Task 3.8. The section is
kept, empty, because registering a decision the day it is proposed is what
stopped the index falling behind the specification a second time.

## Reserved, not yet written

These numbers are reserved by the installation plan. Each needs a business
decision from the product owner before it can be written, and several block
Phase 0 tasks.

| ADR | Title | Blocks |
|---|---|---|
| ADR-003 | Service / selector architecture | Nothing. Was "formalise before Task 0.6"; Task 0.6 shipped over it and the pattern is enforced by CLAUDE.md and by the import-boundary tests (`test_security.py::test_14c_the_api_layer_never_imports_the_kernel_directly`). Write it if a reader ever needs the reasoning; it blocks no work |
| ADR-004 | Append-only ledgers | Nothing. Was "Blocks Task 0.6"; Task 0.6 shipped the behaviour instead — immutability triggers in migrations `accounting.0002`/`0005`, reversal-not-edit in `services.reverse_entry`, traced as ACC-008 and ACC-009 |
| ADR-005 | Moving weighted-average costing | Nothing. Phase 1 shipped it; ADR-018 records the valuation decision |
| ADR-009 | Arabic, RTL, and PDF strategy | Phase 7 reporting. Genuinely unwritten |

## Open questions that must be answered before the ADRs above can be written

Sourced from `docs/plans/phase-0-claude-code-prompts.md`. Items 1–3 were
listed here as unanswered until the Phase 2 gate; all three had been settled
in code for many tasks, and are struck through rather than deleted so the
question and its answer stay together.

1. ~~**Cost center scope** — organization-wide or branch-scoped?~~
   **Settled: organization-wide.** The branch dimension already rides on the
   journal line, so scoping the cost centre to a branch as well would record
   one fact twice. ADR-015 §Settled; ACC-004.
2. ~~**The full chart of accounts**, and per-organization or global codes?~~
   **Settled: `seed_chart_of_accounts` is the list** (77 accounts as of Task
   2.15) and codes are **unique per organization**, enforced by
   `account_code_unique_per_organization`. ADR-014 §Settled; ACC-013, ACC-014.
3. ~~**Who may reopen a closed period**, and is a second approver required?~~
   **Settled: `ACCOUNTING_MANAGER` alone, with a mandatory reason**, and no
   second approver in Release 1. `PRM-003` and `PRM-004` trace it; a
   parametrised test proves no other role holds it.
4. **Business day cutoff** — the actual start time for Al-Bunook; whether all
   branches share one cutoff; whether attendance and payroll use the same
   business date as sales. The *schema* is settled (ADR-008); only the values
   are open, and no default is written into any migration.
5. **Inventory valuation scope** — proposed by ADR-018 as `(warehouse, item,
   lot)`, with organization and branch derivable from the warehouse. This is
   the same scope the architecture plan names, stated in its minimal form.
   Awaiting approval.
6. Whether one branch may hold **multiple warehouses** at go-live — the
   Task 1.0 specification assumes yes (Main Store, Kitchen Store,
   Production, and a system In-Transit warehouse per branch). Awaiting
   approval.
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
- **Account roles and domain-owned posting mappings** — ADR-019.
- **Transfer ownership, in-transit valuation, and cross-branch
  accounting** — ADR-020. Goods stay on the source branch's books until
  received; a receipt is valued from its own dispatch allocation, never
  from the pooled in-transit average; a cross-branch receipt posts two
  coordinated journals so each branch stays balanced on its own books.
- **Physical count cutoff, warehouse freeze, and count-adjustment
  valuation** — ADR-021. One cutoff and one book snapshot, fixed when the
  warehouse freezes; `Warehouse.frozen_by_count` is the only statement that a
  warehouse is frozen, held by a lock every posting takes; blind entry by
  construction rather than by hiding columns; maker-checker in four places; a
  gain into an empty position needs an explicitly approved unit cost, and a
  confirmed zero is not an omitted one; an active count blocks closing its
  period.
- **Supplier return valuation and purchase variance treatment** — ADR-022
  (**accepted and implemented**). A supplier return leaves stock at the standing moving average,
  never at the original receipt price, because there are no cost layers
  underneath the average to pick from; the difference against what the supplier
  credits is a purchase return variance. A price variance never restates a
  posted movement, because the average is a function of posting order and
  repricing would restate closed periods. Revaluation of stock still on hand is
  explicit and permissioned, never automatic.
- **GRNI clearing and three-way matching allocations** — ADR-023
  (**accepted and implemented**). The GRNI balance equals the value of accepted receipt lines no
  invoice has matched, and that is a testable equality. Matching is allocation
  rows, not a status field, because it is genuinely many-to-many and partial;
  matching status is derived and never stored. Over-allocation is refused under
  a row lock and re-verified by reconciliation. An invoice whose goods line has
  no match **refuses to post** (`invoice_awaiting_matching`) and is reported by
  the invoice-without-receipt report — this line previously said the opposite,
  which ADR-023 §5's own amendment had already overturned.
- **Custom roles and a navigation that shows only what the reader may do** —
  ADR-034 (**accepted**). An organization defines its own posts as permission
  sets granted through the same memberships and `role:` groups ADR-016 built;
  a section is shown when the reader holds the permission its view requires,
  derived from the view rather than declared twice; `manage_items` is split so
  "may add but not edit" is expressible.
