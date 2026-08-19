# Sales — invariants

The checklist Phase 4 satisfies. Each line is a test or a database guarantee,
not a guideline. Nothing here is optional and none of it may be relaxed to make
a suite pass.

These **extend** `docs/invariants/inventory-invariants.md`,
`docs/invariants/procurement-invariants.md`,
`docs/invariants/kitchen-invariants.md` and
`docs/specs/accounting-kernel-invariants.md` rather than replace them. A sales
posting that breaks an accounting-kernel invariant is broken twice, because a
sales day is a journal before it is anything else.

**Status: ENFORCED as of checkpoint 7, 2026-08-19.** Every row below cites where
it is kept. Where the enforcement is a database constraint or a trigger, that is
named instead of a test — an `EXCLUDE USING gist` *is* the enforcement, and
pointing at a test that merely observes it would be the weaker claim.

---

## 1. Master data

| # | Invariant | Enforced where |
|---|---|---|
| S-01 | A menu item code is canonical uppercase and unique per organization; an archived code stays reserved | service `_require_code` + `UniqueConstraint`; `PROTECT` everywhere |
| S-02 | A `RECIPE_SERVING` item names a recipe **and** a serving code that some version of it offers | service validation + `verify_sales.verify_menu` |
| S-03 | `DIRECT_STOCK` is declared and refused; there is no second stock-consumption path beside production | service refusal, `direct_stock_deferred`; `verify_menu` reports any row carrying it |
| S-04 | Two active prices never overlap within one scope, for one item, branch and channel or application | `EXCLUDE USING gist` in migration `0002` + `verify_prices` |
| S-05 | Price resolution is **most specific wins** — application, then channel, then branch default — and the rank is explicit, never derived from enum order | `selectors.effective_prices` |
| S-06 | An application channel settles into a receivable and never into a drawer; a non-application channel never settles | two `CheckConstraint`s on `SalesChannel` |
| S-07 | A discount programme's two funding shares add to exactly one hundred | `CheckConstraint` + service + `verify_discount_funding` |
| S-08 | An application-funded share names the application that promised it | `CheckConstraint` `sales_discount_application_funding_names_an_application` |
| S-09 | A commission agreement carries the evidence it rests on | service refusal, `evidence_required` |
| S-10 | Agreements never overlap for one branch and application | `EXCLUDE USING gist` in migration `0004` |

## 2. The sales line

| # | Invariant | Enforced where |
|---|---|---|
| S-11 | Every resolved identity is **stored, not re-derivable**: recipe, exact version, serving, price version, agreement, and every field the commission used | `SalesDayLine` columns (ADR-024) |
| S-12 | The recipe version is the one effective on the **business date**, never the newest | `day_services._effective_version` |
| S-13 | A serving never falls back to the primary one; a missing serving code refuses the line | `day_services._serving_on`, `no_serving_on_effective_version` |
| S-14 | `gross = quantity × unit_price`, quantized once | `resolve_line` + `verify_line_arithmetic` |
| S-15 | `customer_charge = gross − restaurant_discount − application_discount` | as above |
| S-16 | `net = gross − restaurant_discount − commission − other_fees` for an application line, `gross − restaurant_discount` otherwise — which is what makes the journal balance by construction | as above |
| S-17 | An application line names an application **and** an agreement, or neither | `CheckConstraint` `sales_line_application_and_agreement_travel_together` |
| S-18 | A manual discount carries a reason | `CheckConstraint` + service |
| S-19 | Quantity is `Decimal`; nothing anywhere special-cases a half | field type; RCP-082 |

## 3. The journals

| # | Invariant | Enforced where |
|---|---|---|
| S-20 | **Revenue is credited gross.** Σ posted `gross_amount` equals the revenue accounts' net credit, with no netting | `posting.build_plan` + `verify_revenue_is_gross` |
| S-21 | The **application-funded** discount reaches no account at all: it is stored, reported, and never posted | `build_plan` omits it; `verify_application_discount_never_posts` asserts `SALES_DISCOUNT` holds exactly the restaurant share |
| S-22 | A posted day has exactly one `POSTED` journal at `SALES.SALESDAY`, and it still agrees with a rebuilt plan | `verify_day_journals` |
| S-23 | An adjustment posts to `SALES_RETURNS` and **never** touches `SALES_REVENUE` | `adjustment_posting.build_adjustment_plan` + `verify_adjustment_journals` |
| S-24 | All three adjustment reason kinds post the **same** journal; they differ in what they may touch, not in where they land | one plan builder; `test_sales_adjustments.py` |
| S-25 | A settlement journal contains **no** class-6 line. Commission is recognised once, at the sale | `verify_settlement_journals` — the check ADR-028 §6 asks for by name |
| S-26 | A cashier closing posts the approved cash over/short variance and **nothing else** — no revenue, no card clearing, no receivable | `shift_posting.build_shift_plan` + `verify_shift_journals` |
| S-27 | A variance of exactly zero posts **no journal at all** and still takes a number; that is a legitimate outcome, not a missing journal | `approve_cashier_shift` + `verify_shift_journals` |
| S-28 | Every cost centre comes from the **channel**; a sale never invents one and neither does its reversal | `_cost_center_for`; refusal `cost_center_required` |
| S-29 | Every sales journal carries a complete source identity — organization, type, id, `SourceEvent` — or none of it | ADR-017 mechanics + `verify_source_identity` |
| S-30 | A source document type is stored **upper-case**; the constants are spelled the way they are stored | the four `SOURCE_DOCUMENT_TYPE` constants + `verify_source_identity` |
| S-31 | Idempotency keys are **derived** from the document's `public_id`, never accepted from a caller | every posting service; unique per organization |

## 4. The receivable subledger

| # | Invariant | Enforced where |
|---|---|---|
| S-32 | `ApplicationReceivableEntry` is append-only; there is no balance field anywhere in the module | `sales_receivable_is_append_only` trigger |
| S-33 | Exactly one side of an entry is non-zero and positive | `CheckConstraint` `sales_receivable_exactly_one_side` |
| S-34 | One entry per economic event | `UniqueConstraint` on `(organization, application, source, type, id)` |
| S-35 | The subledger equals its **control account**, aggregated per account and not per application | `verify_receivable_ledger` |
| S-36 | `ReceivableSource` is a closed set of five; an adjustment reversal names itself in the document id with `:REVERSED` rather than inventing a sixth | ADR-027 §5 + `REVERSAL_RECEIVABLE_ID_SUFFIX` |
| S-37 | No aging bucket is ever written off automatically | there is no such code path; ADR-028 §9 |

## 5. Settlements

| # | Invariant | Enforced where |
|---|---|---|
| S-38 | Expected, statement and remitted are kept as **three figures**, never one net variance | `ThreeWay`; the detail screen and the API both carry all three |
| S-39 | Every dinar of each gap is claimed by an adjustment carrying that gap's leg; reconciliation is refused otherwise, exactly and without tolerance | `reconcile_settlement`, `unexplained_variance` + `verify_settlement_allocations` |
| S-40 | Both leg equations are **re-checked at posting**, under the row lock | `post_settlement` |
| S-41 | `UNEXPLAINED_APPROVED` costs an explanation and a named approver | two `CheckConstraint`s |
| S-42 | No receivable entry is allocated beyond what it owes, across posted settlements | `sales_settlement_allocation_is_within_its_entry` trigger + `verify_settlement_allocations` |
| S-43 | One settlement per statement reference per application | `UniqueConstraint` |
| S-44 | `statement_commission_amount` is compared and **never posted**; the gap is an `ADVISORY` | `verify_settlement_commission` |

## 6. The till

| # | Invariant | Enforced where |
|---|---|---|
| S-45 | One shift per branch per business date in Release 1 | `UniqueConstraint` `sales_shift_unique_per_branch_and_date` |
| S-46 | **The approver is never the closer**, at the database as well as in the service | `sales_shift_approver_is_not_the_closer` + `approve_cashier_shift` |
| S-47 | A shift closes only against a `POSTED` day; an expectation derived from a draft is a moving target | `close_cashier_shift`, `day_not_posted` |
| S-48 | Counted and expected figures freeze at `CLOSED` | `sales_shift_is_frozen` allowlist trigger |
| S-49 | `APPLICATION_RECEIVABLE` is not countable in a drawer | `CheckConstraint` `sales_shift_tender_is_countable` |
| S-50 | The opening float is counted and never posted; it is neither revenue nor an economic event | `expected_cash_for` uses it, `build_shift_plan` does not |

## 7. Corrections and immutability

| # | Invariant | Enforced where |
|---|---|---|
| S-51 | A posted day, adjustment, settlement and shift are each frozen by a **whole-row allowlist** trigger, never a blocklist | migrations `0006`, `0008`, `0010`, `0012` |
| S-52 | A correction is a reversal plus a replacement; nothing posted is ever edited or deleted | no edit route, no delete route, and the triggers above |
| S-53 | No adjustment takes back more quantity or gross than its original line carried | `sales_adjustment_line_is_within_its_original` trigger + `verify_adjustments_are_within_their_originals` |
| S-54 | A `FINANCIAL_CORRECTION` moves money and **no quantity** | the same trigger; a money correction is not a claim that less food was sold |
| S-55 | **Every adjustment reduces.** A correction that increases what was charged is a new sales day | `sales_adjustment_line_gross_is_positive` and the service |

## 8. The kitchen boundary

| # | Invariant | Enforced where |
|---|---|---|
| S-56 | Only `CANCELLED_BEFORE_FULFILLMENT` reduces theoretical consumption. A return was cooked and its ingredients left | `consumption_source.cancelled_quantities` — one filter long — + `verify_theoretical_quantities` |
| S-57 | The kitchen imports nothing from sales; sales registers itself at app-ready | `register_theoretical_source` |
| S-58 | With the adapter registered, no surface reports `SALES_NOT_INCLUDED_PHASE_4` | `coverage_code()` + `verify_coverage` |
| S-59 | A sale moves no stock; the ingredients left through the batch that cooked them | there is no stock-moving code path in `apps/sales` |

## 9. Authorization and disclosure

| # | Invariant | Enforced where |
|---|---|---|
| S-60 | Out of scope is **404**; in scope without authority is **403** | `OutOfScope` / `PermissionMissing`, every selector and every route |
| S-61 | Every identifier is resolved **with** the caller; no out-of-scope object ever exists in a local variable | `apps/sales/selectors.py` |
| S-62 | Seventeen permissions, every declared name granted, every grant migrated | `verify_permission_scope` |
| S-63 | Cost, margin and food-cost figures are **omitted, never blanked**, without `view_sales_cost` | the dashboard's card registry; `/dashboard/cost` is a separate route that answers 403 |
| S-64 | Reversal of a posted day, adjustment or shift requires `reverse_daily_sales`, organization-wide | every transition view and every API command |
| S-65 | A settlement's reversal uses `manage_application_settlements`, because its migrated label says "post **and reverse**" | `settlement_views` and `api.post_settlement_reverse` |

## 10. Reporting and the API

| # | Invariant | Enforced where |
|---|---|---|
| S-66 | No `float` appears in any dashboard aggregate; shares are exact `Decimal`s quantized once | `apps/sales/dashboard.py` |
| S-67 | `net_revenue` is the ledger's own arithmetic — revenue credit less discount and returns debits — so it can be found in the general ledger | `headline_for` + `test_net_revenue_is_the_ledgers_own_arithmetic` |
| S-68 | Shortage and overage are reported separately and never netted | `CashierSummary` |
| S-69 | A line with no cost snapshot behind it is **counted and reported, never costed at zero** | `cost_summary` |
| S-70 | Money, quantities and rates cross the API as exact **strings**, both directions | `apps/sales/api.py`; asserted by walking whole payloads |
| S-71 | No `PATCH` and no `DELETE` on anything that has left `DRAFT` or `OPEN` | the router's shape; asserted as 405 |
| S-72 | `verify_sales` reports and refuses to repair. There is no `--fix` | RCP-050; asserted on the parser |
| S-73 | The daily reconciliation stores nothing and offers no acknowledge control | `daily_reconciliation.py` has no model and `report_views.py` no POST |

---

## What is deliberately **not** an invariant here

**A tolerance on a settlement gap.** ADR-028 §7 requires exactness, and a
tolerance is where a misconfigured commission rate lives.

**A commission gap being an error.** A rate dispute is a commercial fact between
two companies. A verifier that failed on one would be red every month and
therefore ignored every month.

**A stored daily reconciliation.** There is no fact it would record that the
documents do not already carry, and the stored copy is always the one that goes
stale (spec §2, assumption §8.5).

**A second shift per branch per day.** Release 1's sales granularity is a day,
so a second shift would have no principled share of the day's cash and the
variance — the only number the document exists to produce — would stop meaning
anything. The unique constraint is what gets relaxed when shift-level sales
arrive, and the relaxation will be visible.
