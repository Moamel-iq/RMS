# Inventory — enforced invariants

The checklist Phase 1 must satisfy. Each line is a test, not a guideline.
Nothing here is optional and none of it may be relaxed to make a suite pass.

Modelled on `docs/specs/accounting-kernel-invariants.md`, which these extend
rather than replace: an inventory posting that breaks an accounting invariant
is broken twice.

**Status: approved 2026-08-09.** Task 1.1 delivers master data and
authorization (invariants 4, 5, 15, 16, 17 and the master-data guards below);
the ledger invariants land in Task 1.2 onward.

## The thirty-five

| # | Invariant | Enforced where | Delivered by |
|---|---|---|---|
| 1 | Posted stock movements are immutable; corrections are reversal plus replacement | Trigger `inventory_stockmovement_no_change` (allowlist of permitted deltas, per migration `accounting/0005`) | 1.2 |
| 2 | Quantity is authoritative in the item's base stock unit | Non-null `base_quantity` on every movement | 1.2 |
| 3 | Every posted effect retains entered quantity, entered unit, conversion, conversion version, factor, and base quantity | Non-null columns on `StockMovement` | 1.2 |
| 4 | No float anywhere — quantities, factors, unit costs, values, ratios | `apps/core/quantity.py`, `apps/core/money.py` | 1.1 |
| 5 | Quantity 3 dp stored, 6 dp intermediate, factors 12 dp, IQD 3 dp, unit cost 6 dp, `ROUND_HALF_UP` | Existing core constants; inventory adds none | 1.1 |
| 6 | Valuation key is `(warehouse, item, lot)`; organization and branch are derivable | `UniqueConstraint(..., nulls_distinct=False)` on `StockBalance` — never plain SQL NULL semantics, which would allow unlimited rows for a non-lot item | 1.2 |
| 7 | Moving weighted average is the Release 1 costing method, behind a strategy boundary | `ValuationLayer` / `ValuationAllocation` recorded from day one | 1.2 |
| 8 | Negative stock is refused by default, **including on a reversal that decreases stock** | Service check inside the row lock. A plain `CHECK (quantity >= 0)` is **not** usable — an authorised override legitimately produces one — so the database layer is a reconciliation test that every negative balance traces to an authorised override | 1.2 |
| 9 | A negative-stock override needs permission, reason, actor, audit event, and an exception report. **No permanent per-item flag exists** | `inventory.override_negative_stock` + `PERMISSION_OVERRIDE` event | 1.2 |
| 10 | Balance rows are locked before the availability check, in deterministic primary-key order | `select_for_update().order_by("pk")` | 1.2 |
| 11 | `StockBalance` is a projection and rebuilds exactly from the movement ledger | Rebuild command + reconciliation test | 1.2 |
| 12 | Authoritative first posting is synchronous and atomic — never a background job | `transaction.atomic()` in the posting service | 1.2 |
| 13 | Every movement carries organization, branch, warehouse, item, signed base quantity, signed value, effective timestamp, actor, source identity, idempotency identity, posted timestamp, movement type, and valuation snapshot | Non-null columns | 1.2 |
| 14 | A movement's effective date respects accounting period closure | `validate_period_accepts_postings` reused, not reimplemented | 1.2 |
| 15 | Organization and branch access go through the Phase 0 authorization layer; a submitted id never widens access | `apps/organizations/authorization.py` | 1.1 |
| 16 | No writable CRUD path bypasses the posting services | Command API + read-only admin + import-boundary test | 1.1 |
| 17 | Technical identifiers and factors are locale-independent | Existing `factor_display` pattern | 1.1 |
| 18 | Quantity zero implies value zero, by construction | Full-depletion rule: the depleting movement takes the entire remaining value | 1.2 |
| 19 | No residual is hidden by mutating a historical movement | Immutability trigger; residual absorbed at post time | 1.2 |
| 20 | One economic event produces one stock effect | Source identity + organization-scoped idempotency key | 1.3 |
| 21 | Inventory valuation reconciles to the inventory control account | Reconciliation report and test | 1.3 |
| 22 | Sum of a warehouse's location quantities equals its warehouse quantity | Reconciliation test | 1.7 |
| 23 | An item code is canonical uppercase and unique per organization; archived codes stay reserved | `strip().upper()` in the service + `UniqueConstraint` | 1.1 |
| 24 | Categories: no cycles, depth ≤3, items on leaves only, a category with items gains no children, a category with children takes no items | Service guards + constraints | 1.1 |
| 25 | A package unit never carries a universal conversion factor | `PackageUnit` has **no factor field** | 1.1 |
| 26 | Every package conversion resolves directly to the item's base unit; no chains | `ItemPackageConversion.factor_to_base` | 1.1 |
| 27 | At most one active default purchase package per item | Partial unique index | 1.1 |
| 28 | Overlapping effective conversion periods for one (item, package) are impossible | `EXCLUDE USING gist` | 1.1 |
| 29 | `organization`, `base_unit`, `tracks_lots`, `costing_method` are immutable once movements exist | Service guard | 1.1 (guard) / 1.2 (movements) |
| 30 | Warehouse scope: `SELECTED` grants only listed warehouses; `ALL` includes future ones; selections never cross branches | `BranchMembership.warehouse_scope_mode` | 1.1 |
| 31 | A system warehouse cannot be renamed, archived, or converted by a normal user | Service guard | 1.1 |
| 32 | A count approver is never the conductor | `approver_id != conductor_id` | 1.6 |
| 33 | A positive count gain never creates quantity at zero value | Explicit unit cost required where the average is zero or undefined | 1.6 |
| 34 | Source identity is normalised centrally before storage | `strip()`/`upper()` in the accounting service | 1.2 |
| 35 | Every report names its cutoff semantics — effective-date or posted-as-of | Report contract | 1.7 |

## Rules that carry over unchanged from Phase 0

These are not restated as inventory invariants because they already hold and
inventory must not weaken them:

- Posted journal entries are immutable on **every** column (migration
  `accounting/0005`).
- Debits equal credits on stored 3-decimal values, checked at a real COMMIT.
- Nothing posts into a `CLOSED` period; `SOFT_CLOSED` needs an explicit,
  audited override.
- Source identity is complete or absent, and immutable once posted.
- Idempotency keys are unique per organization and verified against a
  fingerprint of the request.
- Audit events are append-only, enforced by a database trigger.
- `previous_state` is the authoritative persisted state before the mutation.

## Deliberate non-invariants

Recorded so that nobody later "fixes" them:

- **Past valuation is not restated by a backdated posting.** Quantity as-of a
  past date is exact; value as-of a past date is the value that was known
  then. Restating it would rewrite posted, reported, reconciled movements.
  Approved 2026-08-09; see ADR-018 §5.
- **No periodic revaluation engine in Release 1.** An explicit approved manual
  revaluation is sufficient until a real case demands otherwise.
- **Locations carry no value.** A bin move is not a revaluation.
- **`RETURN_IN` is valued at the original issue's cost**, not the current
  average, so returning unused stock creates no gain or loss.
- **A reversal is valued at its original's value**, not the current average.
  It is nevertheless **subject to the availability check** when its mirror
  decreases stock — see invariant 8.
- **FEFO/FIFO picking is not implemented.** Lot selection is manual in
  Release 1.
