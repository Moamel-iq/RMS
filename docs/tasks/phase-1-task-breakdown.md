# Phase 1 — Inventory: task breakdown and exit gates

Proposed by Task 1.0. The order is dependency-driven, not size-driven, and the
governing principle is the same one that made Phase 0 work: **the ledger must
be trustworthy before anything is allowed to depend on it.**

## Why this shape

Two ordering constraints do most of the work.

**The valuation engine comes before anything that creates value.** Receipts,
issues, transfers, and waste are all *callers* of the moving-average
algorithm. Building any of them first means building the algorithm inside a
workflow, and then extracting it — which is how a second, slightly different
copy of it appears later.

**Opening stock comes before receipts, not after.** Opening stock is the only
movement that legitimately creates quantity from nothing, so it is the
simplest possible exercise of the full path: document → movement → balance →
valuation → journal entry → reconciliation. It proves the whole spine with one
movement type before four more are added on top of it.

## The tasks

### Task 1.0 — Domain specification *(this task)*

Specification, invariants, task breakdown, decision table. No code.

**Exit:** the decision table is approved, or amended and then approved.

---

### Task 1.1 — Master data: categories, items, conversions, warehouses

`ItemCategory`, `InventoryItem`, `ItemUnitConversion`, `Warehouse`,
`BranchItemSetting`, the 18 permissions with organization/branch/**warehouse**
scope, the command API, and the first four native screens.

Depends on: 1.0 decisions 1–5, 7, 14.

**Exit gate**

- Item code unique per organization; archived codes reserved.
- Leaf-only category rule enforced with a test for each direction.
- `base_unit` cannot change once movements exist (tested against a movement).
- `FIXED` and `VARIABLE` conversions both validated; overlap refused by the
  exclusion constraint.
- Warehouse scope enforced; a foreign branch's warehouse is a **404**.
- No float in storage or transport; decimals as exact strings both ways.
- Screens render inside the shell, RTL, no Django admin for normal users.

---

### Task 1.2 — Stock movement ledger, balance projection, valuation engine

The immutable `StockMovement`, the `StockBalance` projection, `ValuationLayer`
and `ValuationAllocation`, the moving-average algorithm, negative-stock
prevention, locking, the rebuild command, and the `source_document_id`
normalisation from spec §1.

**The heart of Phase 1.** Everything after it is an application of it.

Depends on: 1.1; decisions 5, 6, 8, 9.

**Exit gate**

- All 18 valuation cases from spec §9 tested individually.
- Quantity zero implies value zero, proven including the divergent case.
- Rebuild equals ledger replay, including after concurrent load.
- Concurrency plan §10 green — all five tests, at a real COMMIT.
- Movement immutability trigger uses an **allowlist**, per `accounting/0005`.
- Closed-period posting refused; soft-closed needs the audited override.
- No background job anywhere on the authoritative posting path.

---

### Task 1.3 — Account mapping, opening stock, inventory ↔ GL reconciliation

First the `AccountRole` / `AccountMapping` resolver in `apps/accounting`
(spec §11 — **it does not exist today**), then the opening-stock document and
its atomic journal posting, then the reconciliation report.

Depends on: 1.2; decisions 10, 11, 13.

**Exit gate**

- No hard-coded account id or code anywhere in inventory posting.
- `resolve_account` raises `account_role_unmapped` rather than guessing.
- Opening posts quantity, valuation, and accounting in **one** transaction.
- Opening value equals its journal entry exactly (AT-002).
- Inventory control balance reconciles to inventory valuation (AT-002).
- Duplicate source event cannot double-post; key + changed payload conflicts;
  same key in another organization is independent (AT-009).
- Mixed opening dates refused.

---

### Task 1.4 — Receipts, issues, returns, and reversal

`RECEIPT`, `ISSUE`, `RETURN_IN`, `RETURN_OUT`, `REVERSAL`.

Manual receipts only. **This is not procurement** — no supplier, no purchase
order, no invoice, no payable. A receipt here records what physically entered
a warehouse and nothing about who is owed for it.

Depends on: 1.3.

**Exit gate**

- Reversal restores quantity and value exactly, and is value-neutral.
- `RETURN_IN` values at the **original issue's** cost, linked to it.
- Negative stock refused on every outbound path.
- Every movement type's accounting matches the spec §8 table.

---

### Task 1.5 — Transfers, in-transit, shortages

`TRANSFER_DISPATCH`, `TRANSFER_RECEIPT`, `TRANSFER_SHORTAGE`, and the
`IN_TRANSIT` system warehouse.

Depends on: 1.4.

**Exit gate**

- Dispatch value reconciles exactly to receipt plus shortage (AT-002).
- A transfer creates no gain or loss from movement alone.
- Inter-branch transfer requires `post_transfer` at **both** branches.
- `IN_TRANSIT` is not user-creatable and accepts no other movement type.

---

### Task 1.6 — Waste, counts, adjustments

`WASTE`, `COUNT_ADJUSTMENT`, `MANUAL_ADJUSTMENT`, and the `HARD_FREEZE` count
workflow with blind counting and separated approval.

Depends on: 1.4.

**Exit gate**

- Posting to a frozen warehouse refused, naming the count.
- Book snapshot immutable from cutoff; variance computed, never entered.
- Conducting and approving a count are different permissions, tested.
- Reason code mandatory on waste and manual adjustment.

---

### Task 1.7 — Locations, import, reports, rebuild tooling, security hardening

Optional `StockLocation`, the import boundary with atomic rollback, stock and
valuation reports, the rebuild/reconcile management commands, and an inventory
security sweep mirroring Task 0.7's.

Depends on: 1.5, 1.6.

**Exit gate**

- Import rollback is atomic — a failing row leaves nothing behind (AT-012).
- Location quantities sum to warehouse quantity (invariant 22).
- A fresh database receives all required inventory reference data.
- Cross-organization and cross-branch sweep green (AT-008).

---

### Task 1.8 — Phase 1 exit gate

The equivalent of Task 0.8: fresh-database reproducibility, an end-to-end
integration path, the full security gate, documentation consistency, and a
PASS/FAIL/DEFERRED matrix.

**Exit gate**

- Every invariant in `docs/invariants/inventory-invariants.md` enforced and
  tested.
- All 28 tests in spec §14 green.
- Fresh database migrates from zero and seeds correctly.
- Full suite, ruff, ruff format, mypy clean; no pending migrations.
- Inventory reconciles to the general ledger on a populated database.

## Where this differs from the suggested shape, and why

Two changes, both dependency-driven:

**The account-mapping resolver moved into Task 1.3** rather than being assumed
to exist. It does not exist — verified, not inferred — and opening stock
cannot post without it. Leaving it implicit would have meant discovering it
mid-task and either hard-coding an account or stalling.

**`StockLocation` moved from 1.1 to 1.7.** Locations are optional, carry no
value, and nothing in receipts, issues, transfers, counts, or valuation
depends on them. Building them early adds a dimension to every query and every
test for no Release 1 benefit, and the warehouse-level ledger has to be right
first regardless.
