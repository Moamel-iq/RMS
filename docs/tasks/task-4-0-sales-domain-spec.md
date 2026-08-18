# Task 4.0 — Sales domain specification

- **Status**: Approved under the owner's Phase 4 decisions
- **Date**: 2026-08-19
- **Phase**: 4 — Sales
- **Decisions**: ADR-027 (recognition and application receivables), ADR-028
  (discounts, commissions and settlements)
- **Base**: `origin/phase/3-kitchen` at `526397b`

> **Certification note.** The `phase-3-kitchen-complete` tag did not exist when
> this branch was created, so `phase/4-sales` was cut from the latest clean
> pushed `origin/phase/3-kitchen` instead. **Sales certification must rebase
> onto the Phase 3 completion tag before the Phase 4 exit gate.** Recorded here
> rather than left in a commit message, because a rebase requirement that lives
> only in someone's memory is a rebase that does not happen.

---

## 1. The audit that came first

The repository was searched for `MenuItem`, `SalesChannel`, `DeliveryApp`,
`DeliveryPlatform`, `SalesDay`, `DailySales`, `SalesInvoice`, `SalesReturn`,
`CashierShift`, `CashierClosing`, `AppSettlement`, `AppReceivable`,
`CommissionAgreement`, `DiscountProgram`, `SalesReconciliation` and
`verify_sales` before anything was written. The inert navigation was **not**
taken as evidence of absence — Phase 2 taught that lesson, where two of three
"missing" procurement features already existed in another shape.

| Area | Classification | Evidence |
|---|---|---|
| Menu master | **genuinely absent** | no model, no table, no route. The only occurrence of `MenuItem` in the tree is `apps/inventory/tests/test_master_data.py:377`, which asserts that inventory does *not* define one |
| Sales channels | **genuinely absent** | — |
| Delivery applications | **genuinely absent as a model**; the *chart* already anticipates them (`1-02-01-001..003`) | `seed_chart_of_accounts.CHART` |
| Agreements, discounts | **genuinely absent** | — |
| Daily sales, returns | **genuinely absent** | — |
| Receivables, settlements | **genuinely absent as models**; receivable accounts exist | as above |
| Cashier shift and closing | **genuinely absent** | `Role.CASHIER` exists and carried **no permissions in any module** |
| Daily reconciliation | **genuinely absent** | — |
| Sales revenue accounts | **already implemented** | `4-01-01-001`, `4-01-01-002`, `4-01-02-001` |
| Kitchen quantity source | **interface exists, adapter absent by design** | `apps/kitchen/consumption_sources.py` — `TheoreticalSourceType.SALES` declared, `REGISTERED_SOURCES` deliberately excludes it |
| Sales navigation | **twelve inert sections already declared** | `apps/core/navigation.py` |

So: one genuinely new domain, one existing interface to plug into, and an
existing chart to extend. No duplicate model, ledger, posting service or route
was created, and nothing in Inventory, Procurement or Kitchen production was
redesigned.

---

## 2. Aggregates

```
MenuCategory
MenuItem ────────────── MenuItemBranchSetting
   │                     MenuPriceVersion  (effective-dated, scoped)
   └── Recipe / RecipeServing        (apps.kitchen, read only)

SalesChannel
DeliveryApplication ─── DeliveryApplicationBranchSetting
   └── DeliveryAgreement            (effective-dated)
DiscountProgram

SalesDay ────────────── SalesDayLine
   │                     SalesTenderSummary
   └── SalesAdjustment ── SalesAdjustmentLine

ApplicationReceivableEntry           (append-only)
DeliveryApplicationSettlement ─────── …Allocation
                                      …Adjustment
CashierShift ────────── CashierTenderCount
```

`SalesDailyReconciliation` is deliberately **not** a model. It is a report
computed from the above, because there is no fact it would record that the
documents do not already carry, and a stored reconciliation would be a second
place for the same truth to live.

---

## 3. Fulfillment source

Release 1 sells `RECIPE_SERVING` menu items: a `MenuItem` names one `Recipe`
and one `RecipeServing`, and the sale resolves the `RecipeVersion` in force at
the business date.

`DIRECT_STOCK` is declared in the vocabulary and **refused** by the service.
There is no certified sales-and-COGS route out of a warehouse, and inventing
one would create a second stock-consumption path beside production. Declaring
the value now means adding it later is data plus a service branch, not a
redesign of the line.

Quantity is `Decimal`. `0.500`, `1.000` and `2.500` are all ordinary values,
and nothing anywhere special-cases a half. The serving row carries the
fraction; no code names a chicken.

---

## 4. Prices

`MenuPriceVersion` is effective-dated, and its scope is one of:

| Scope | Meaning |
|---|---|
| `BRANCH_DEFAULT` | what this branch charges unless something narrower says otherwise |
| `CHANNEL` | this channel charges differently |
| `APPLICATION` | this delivery application charges differently |

Resolution is **most specific wins**: application, then channel, then branch
default. Overlapping effective ranges *within the same scope* are refused by an
exclusion constraint, so "which price applies" always has exactly one answer
and the answer is a database guarantee rather than an ordering convention.

---

## 5. Accounting

Eleven roles, seeded by `accounting.0015_sales_account_roles`, all
organization-scoped. Per-application and per-channel overrides live on the
sales master data that owns the concept — accounting never learns what a
delivery application is (ADR-019).

| Role | Default account |
|---|---|
| `SALES_REVENUE` | `4-01-01-001` |
| `SALES_DISCOUNT` | `4-02-01-001` |
| `SALES_RETURNS` | `4-03-01-001` |
| `SALES_CASH_ON_HAND` | `1-01-01-001` |
| `SALES_CARD_CLEARING` | `1-01-03-001` |
| `DELIVERY_APP_RECEIVABLE` | `1-02-01-009` |
| `DELIVERY_COMMISSION_EXPENSE` | `6-03-01-001` |
| `DELIVERY_OTHER_FEE_EXPENSE` | `6-03-01-002` |
| `DELIVERY_SETTLEMENT_VARIANCE` | `7-09-05-001` |
| `SALES_SETTLEMENT_BANK` | `1-01-02-001` |
| `SALES_CASH_OVER_SHORT` | `7-09-06-001` |

Classes 4, 5 and 6 require a cost centre, so every revenue, discount,
commission and fee line carries one. It comes from the **channel**, which is
where the business meaning is: dine-in earns in the hall, application orders
earn in delivery. A sale never invents a cost centre and never posts without
one.

`SALES_SETTLEMENT_BANK` is the one role beyond the owner's §E10 list. It is
required by the §E8 settlement journal, which debits "Bank / Cash": the cash
half is `SALES_CASH_ON_HAND`, and the bank half had no role. Recorded here
rather than quietly added.

The journals are specified in ADR-027 §6–§8 and ADR-028 §6.

---

## 6. Permissions

Seventeen, in `apps/sales/permissions.py`, each with an explicit scope.
`Role.CASHIER` receives its first permissions in the entire system here, which
is worth noting: until Phase 4 the role existed and granted nothing anywhere.

Out of scope answers **404**; in scope without authority answers **403**
(ADR-016). A global Django group without a membership reaches nothing.

---

## 7. Kitchen integration

`apps.sales.consumption_source.SalesQuantitySource` implements the Phase 3
`TheoreticalConsumptionSource` protocol and registers itself at app-ready
through a new `apps.kitchen.consumption_sources.register_theoretical_source()`.

The kitchen imports nothing from sales. Coverage codes stop being module-level
constants and become **computed from the registry**, so a deployment without
sales still reports `SALES_NOT_INCLUDED_PHASE_4` honestly and a deployment with
it reports a final figure.

Only `CANCELLED_BEFORE_FULFILLMENT` reduces the contributed quantity. See
ADR-027 §9 and ADR-028 §8 for why a return does not.

---

## 8. Assumptions recorded honestly

No separate approval round was taken for these; the owner's Phase 4 prompt
settled the policy and these are the readings applied.

1. **`SUBMITTED` is a real state for a sales day** even though the supplier
   invoice has none. Reasoning in ADR-027 §3.
2. **Commission bases `AFTER_ALL_DISCOUNTS` and `CUSTOMER_PAID_AMOUNT` are
   numerically identical in Release 1** and are still kept as separate values.
   ADR-028 §4.
3. **The cost centre comes from the channel.** Nothing in the prompt named a
   source; the channel is the only master with the right granularity.
4. **A menu item with no recipe version effective at the business date cannot
   be sold that day.** The service refuses rather than falling back.
5. **`SalesDailyReconciliation` is report-only**, not a persisted aggregate.
   The prompt permits either; nothing needs persisting.
6. **Demo delivery applications are fictional.** No real contract, rate or
   company name is used as approved data.

---

## 9. Out of scope for Phase 4

HR, Payroll, Treasury, Phase 5 accounting reports, any Kitchen or Inventory
redesign, direct-stock sales, tax and VAT, point-of-sale integration, customer
masters, tips, and driver payments. Each is named in ADR-027 §10 or ADR-028 §9
with the reason.
