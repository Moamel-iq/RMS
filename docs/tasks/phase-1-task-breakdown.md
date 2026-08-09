# Phase 1 — Inventory: task breakdown and exit gates

Approved 2026-08-09. The order is dependency-driven, not size-driven, and the
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

### Task 1.0 — Domain specification — **COMPLETE**

Specification, invariants, task breakdown, decision table. No code.

**Exit met:** all fourteen decisions approved with amendments on 2026-08-09.

---

### Task 1.1 — Master data: categories, items, conversions, warehouses

`ItemCategory`, `PackageUnit`, `InventoryItem`, `ItemPackageConversion`,
`BranchItemSetting`, `Warehouse`, the `BranchMembership` warehouse-scope
extension, the 18 permissions with organization/branch/**warehouse** scope,
service-only master-data writes, the API, and five native screens.

Depends on: 1.0 decisions 1–5, 7, 14.

**Exit gate**

- Item code unique per organization; archived codes reserved.
- Leaf-only category rule enforced with a test for each direction.
- `base_unit` cannot change once movements exist (tested against a movement).
- `PackageUnit` carries no factor; conversions resolve **directly** to the
  item's base unit with no chaining.
- `FIXED` and `VARIABLE` conversions both validated; overlap refused by the
  exclusion constraint; one default purchase package per item.
- Category guards: cycles, depth >3, non-leaf items, items-then-children, and
  children-then-items all refused.
- Warehouse scope: `SELECTED` restricts, `ALL` includes future warehouses,
  selection cannot cross branches, system `IN_TRANSIT` protected.
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

- Valuation cases 1–5, 8–10, and 13–18 from spec §9 tested individually.
  **Cases 6, 7, 11, and 12 are deferred with their documents**: `RETURN_IN`,
  `RETURN_OUT`, transfer, and transfer shortage are properties of documents
  Tasks 1.3–1.6 create. The movement types exist and the kernel values them;
  what does not exist is the original issue to return against or the in-transit
  leg to dispatch into, so testing them now would test a fiction.
- Quantity zero implies value zero, proven including the divergent case, and
  again as a Hypothesis property over the whole input space.
- Rebuild equals ledger replay; a corrupted projection is detected and the
  command refuses to repair it.
- Concurrency green at a real COMMIT: concurrent issues, concurrent first
  receipts into an absent key, opposite-order multi-key events, in-flight
  identical retries, and a raw duplicate null-lot balance.
- Movement immutability is stricter than an allowlist: `StockMovement` is
  **insert-only**, with no permitted update at all. The entry and layer
  triggers do use whole-row allowlists, per `accounting/0005`.
- **Amended:** posting requires an OPEN period. `SOFT_CLOSED` is refused with
  no override, superseding the earlier "soft-closed needs the audited
  override" line. A stock movement changes what the accounts will say, and a
  period that has stopped accepting entries has stopped accepting the things
  that cause them. The count-adjustment exception arrives in Task 1.6 attached
  to a real count document, never to a flag.
- **Amended:** negative stock is refused for everyone.
  `inventory.override_negative_stock` stays reserved and non-operational until
  a valuation policy for the variance it creates is approved.
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

**Implementation notes (2026-08-09):**

- ADR-019 records the durable architecture: role vocabulary and organization
  defaults in `apps.accounting`; item/category overrides and the resolver in
  `apps.inventory`; dependency direction inventory → accounting only, with a
  guard hook instead of a reverse import.
- "Mixed opening dates refused" is satisfied structurally: the cutoff is a
  **document** field, so a line cannot carry its own date at all.
- The opening's source identity is the document's immutable UUID, not the
  human number — drafts carry no number, and the gapless `OPN-<year>-<n>`
  number is assigned only inside the posting transaction (spec §11 amended).
- The combined posting's lock order is document row → stock keys → stock
  counter → document number → journal number. The document-number step sits
  AFTER the stock counter, deviating from the suggested order, because the
  kernel owns "keys then counter" as one unit; the order is global and
  concurrency-tested (ADR-019).
- Maker-checker (`submitted_by != posted_by`) is enforced in the service and
  by a database constraint; a posted document and its lines are frozen by
  whole-row-allowlist triggers.
- An `INVENTORY_CONTROL` mapping change that would re-home the standing value
  of an item with stock is refused (`inventory_account_reclassification_required`)
  until a real GL reclassification workflow exists — through the override
  services, the accounting default services, and item category moves alike.
- `inventory.override_negative_stock` is now granted to **no role by
  default** while `NEGATIVE_STOCK_OVERRIDE_ENABLED` is False (§B.2 of the
  task brief); the Task 1.2 exit-gate line about the reserved permission
  stands, strengthened.

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
