# Phase 4 — Sales and Settlements: task breakdown and exit gates

Written 2026-08-19, **after** the phase landed rather than before it. Phases 1
to 3 each proposed a breakdown and then reported against it; Phase 4 was worked
as eight checkpoints against `docs/tasks/task-4-0-sales-domain-spec.md` and a
shared contract, and this document records what each checkpoint actually owned.
Saying so is the point: a breakdown backdated to look like a plan would be the
one document in this repository that lies about its own provenance.

The governing principle is the one all three earlier phases proved: **nothing
depends on a figure until the figure is reconcilable.**

## Why this shape

**The menu before the sale.** A sales line resolves a menu item, a price, a
recipe version and a serving, and stores every one of them. Master data before
documents, the Phase 2 ordering applied again.

**The contract before the order it prices.** A delivery agreement decides what
every future application order is worth; a discount decides who funds a
promotion. Both are master data with effective dates, and both had to exist
before a line could accrue anything.

**Posting in its own checkpoint.** Checkpoints 1 and 2 built masters and posted
nothing at all. Checkpoint 3 is the phase's first ledger-touching task and its
first certification boundary.

**Corrections after the thing they correct.** A return needs a posted day to
take back; a settlement needs a receivable to clear; a drawer needs a posted day
to be counted against. Each of checkpoints 4, 5 and 6 depends on 3 and on
nothing later.

**The dashboard, the demo and the verifier last.** All three read everything the
module produces, so all three could only be written once everything existed.

---

### Task 4.0 — Sales domain specification and the account roles

The audit, the specification, ADR-027, ADR-028, and the eleven `SALES` account
roles seeded by `accounting.0015_sales_account_roles`. No model, and
`apps.sales` deliberately not yet in `INSTALLED_APPS`.

**Depends on**: Phase 3 complete.
**Delivered**: `docs/tasks/task-4-0-sales-domain-spec.md`, ADR-027, ADR-028,
`accounting/migrations/0015_sales_account_roles.py`, seventeen chart accounts.

### Task 4.1 — The menu, its prices and the sales channels

`MenuCategory`, `MenuItem`, `MenuItemBranchSetting`, `MenuPriceVersion`,
`SalesChannel`. Effective-dated prices with an exclusion constraint per scope,
so "which price applies" has exactly one answer and the answer is a database
guarantee.

**Depends on**: 4.0, and the kitchen's `Recipe` / `RecipeServing`.

### Task 4.2 — Delivery applications, agreements and discount programmes

`DeliveryApplication`, `DeliveryApplicationBranchSetting`, `DeliveryAgreement`,
`DiscountProgram`. Four commission bases, and a funding split whose two shares
must add to one hundred at the database.

**Depends on**: 4.1.

### Task 4.3 — Daily sales capture, posting and the receivable ledger

`SalesDay`, `SalesDayLine`, `SalesTenderSummary`,
`ApplicationReceivableEntry`, and `posting.post_sales_day`. The first journal
Phase 4 writes, the append-only receivable subledger, and the `SALES`
theoretical-consumption adapter registered with the kitchen.

**Depends on**: 4.2.

### Task 4.4 — Returns, cancellations and financial corrections

`SalesAdjustment`, `SalesAdjustmentLine`, three closed reason kinds, and the
journal that debits `SALES_RETURNS` and never `SALES_REVENUE`. Replaces
`consumption_source._cancelled_quantity` with the query that subtracts
cancellations **and nothing else**.

**Depends on**: 4.3.

### Task 4.5 — Application receivables and settlements

`DeliveryApplicationSettlement` with its allocations and its variance claims,
the three-way comparison, and the two screens over the append-only ledger. An
unexplained gap on either leg blocks reconciliation exactly.

**Depends on**: 4.3.

### Task 4.6 — Cashier closing and the daily reconciliation

`CashierShift`, `CashierTenderCount`, maker-checker at the database and in the
service, and the over/short journal — the only thing a closing may post. The
reconciliation is report-only.

**Depends on**: 4.3.

### Task 4.7 — لوحة المبيعات, the demo, the verifier, the API and the documentation

The dashboard and the twelfth navigation entry; `seed_sales_demo`;
`verify_sales`; `/api/v1/sales/`; and the documentation set. No migration, no
new permission, and no new model — checkpoint 7 reads what the six before it
wrote.

**Depends on**: 4.1 – 4.6.

---

## Exit gates

| Gate | What it proves | Where it is checked |
|---|---|---|
| Migrations clean | Nothing pending after 4.7 | `makemigrations --check --dry-run` |
| Types and style | Ruff and mypy clean over `apps/sales` | the pre-commit hook |
| Routes | Every one of the twelve sections answers 200 as a page **and** as an htmx fragment with no nested shell | the route smoke, run per checkpoint |
| Suite | `pytest apps/sales` green against a database built from the migrations | run without `--reuse-db` at 4.7 |
| Ledger | `verify_sales` reports zero `ERROR` on the demo dataset | `manage.py verify_sales` |
| Demo | A second run creates nothing: every table counted before and after | `apps/sales/tests/test_sales_demo_seed.py` |
| Navigation | Zero `قريباً` badges remain in the Sales module | `apps/core/tests/test_shell.py` |
| Permissions | Seventeen, every declared name granted, every grant migrated | `apps/sales/tests/test_verify_sales.py` |

## The certification note, carried forward

`phase/4-sales` was cut from the latest clean pushed `origin/phase/3-kitchen`
because the `phase-3-kitchen-complete` tag did not exist at the time. **Sales
certification must rebase onto the Phase 3 completion tag before the Phase 4
exit gate.** Recorded in the spec, in the runbook and here, because a rebase
requirement that lives in one place is a rebase that does not happen.
