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
## Implemented by Task 1.2, and where each one lives

The kernel exists now, so each invariant has an address. Where a rule is
enforced twice, both are listed on purpose: the service gives an operator a
sentence they can act on, and the database is what still holds when a repair
script bypasses the service.

| Rule | Service | Database |
|---|---|---|
| Movements are insert-only | never updated | trigger `stock_movement_is_insert_only` |
| Entries are immutable but for the reversal link | `post_stock_entry` | trigger `stock_entry_is_immutable` |
| Source identity complete or absent | `canonical_source_identity` | `stock_entry_source_identity_all_or_none` |
| Source identity immutable once posted | — | trigger, reported separately |
| One posting per economic event | `_entry_for_source` lookup | `stock_entry_source_event_unique_per_organization` |
| Idempotency keys scoped per organization | `_replay` | `stock_entry_idempotency_key_unique_per_organization` |
| One effect per effect key per posting | duplicate check | `stock_movement_effect_key_unique_per_entry` |
| Posted sequence unique per organization | counter row lock | `stock_movement_sequence_unique_per_organization` |
| One balance row per stock position | advisory lock | `stock_balance_key_unique` (`NULLS NOT DISTINCT`) |
| Quantity zero implies value zero | `apply_outbound` | `stock_balance_zero_quantity_has_zero_value` |
| No negative stock in Task 1.2 | `_require_available` | `stock_balance_quantity_not_negative` |
| Denormalised owner matches the warehouse | services | triggers on balance and movement |
| Valuation layer cost is historical | never updated | trigger `valuation_layer_cost_is_historical` |

## Deliberate non-invariants, added by Task 1.2

- **`inventory.override_negative_stock` is reserved and not operational.** The
  kernel consults no permission when refusing a negative position, because the
  moving-average contract does not define how a later receipt settles the
  variance one creates. Activating it later must relax
  `stock_balance_quantity_not_negative` in the same migration.
- **`ValuationAllocation` is empty by design.** A moving average consumes no
  layer; writing allocations would fabricate evidence. The rows are derivable
  from `posted_sequence` if a layered strategy is ever adopted.
- **`ValuationLayer.remaining_quantity` is not a stock figure.** Under moving
  average it stays at the received quantity, because nothing consumed it. The
  balance row is the only authority on what is physically left.
- **The posted sequence may have gaps in future.** It is gapless today because
  the counter is a locked row; if that lock ever binds it will be replaced by
  a PostgreSQL sequence, and nothing may come to depend on contiguity.

## Implemented by Task 1.3, and where each one lives

The first combined inventory + accounting posting. The same double
enforcement discipline: the service explains, the database holds.

| Rule | Service | Database |
|---|---|---|
| System account-role codes are reserved | admin read-only, no rename service | trigger `accounting_system_role_reserved` |
| Mapping ranges cannot overlap (defaults) | `_validate_no_mapping_overlap` | EXCLUDE `org_account_mapping_no_overlapping_periods` |
| Override ranges cannot overlap, NULL targets included | `_validate_no_override_overlap` | EXCLUDE `inventory_mapping_no_overlapping_periods` (COALESCE) |
| An override targets exactly one of item/category | `_validate_override_shape` | `inventory_mapping_one_target` |
| A used mapping is immutable but for closing its range | `mapping_is_used` checks | PROTECT FKs from snapshot columns |
| Resolution never guesses | `resolve_default_account`, `resolve_inventory_account` | — (`account_role_unmapped` before any effect) |
| Standing stock value cannot be re-homed by a mapping change | reclassification guard, both apps via the hook | — (apply-then-verify inside the transaction) |
| One valuation key per opening document | duplicate check | `opening_line_valuation_key_unique` (`NULLS NOT DISTINCT`) |
| Opening lines are positive in quantity, cost, and value | line validation | three CHECK constraints |
| An opening is the first movement for its keys | history check under the advisory locks | — (the locks make the check race-free) |
| The submitter cannot post their own opening | `post_opening_document` | `opening_submitter_is_not_poster` |
| A number exists exactly from the moment of posting | `_next_document_number` | `opening_numbered_iff_posted` + partial unique |
| A posted document is immutable but for its reversal | status checks | trigger `inventory_opening_document_immutable` (allowlist) |
| Lines freeze with their document | DRAFT-only line services | trigger `inventory_opening_line_frozen_with_document` |
| Line value == movement value == journal line share | one stored figure, passed through | reconciliation reports any drift |
| Reconciliation reads history by the account it entered | snapshot on the line | never re-resolved through today's mapping |

## Deliberate non-invariants, added by Task 1.3

- **The reconciliation command repairs nothing.** `verify_inventory_accounting`
  reports and exits non-zero; overwriting a balance or posting a balancing
  journal would erase the evidence it exists to find.
- **A FIFO cutover is possible, not free.** Layers and posted order make past
  allocations computable; adopting a layered strategy is a controlled cutover
  with a rebuild policy, never a configuration toggle (ADR-018 §3, amended).
- **The GL side of the reconciliation sums every journal line on a control
  account, not only inventory-sourced ones.** A manual journal against an
  inventory-control account is exactly the drift the report must surface.

## Implemented by Task 1.4, and where each one lives

The operational documents, and the two cross-cutting rules they forced.

| Rule | Service | Database |
|---|---|---|
| The accounting period is judged on the **business date** | `_validate_period_is_open` | — (one open period per event, never two) |
| A committed business date does not move when a branch cutoff changes | submission/posting snapshot | `opening_business_date_snapshot_present`, `inventory_document_business_date_snapshot_present` |
| A posting and a mapping mutation cannot interleave | shared vs exclusive advisory lock | — (`pg_advisory_xact_lock[_shared]`, ADR-019 §5) |
| A mutation takes the advisory lock before any row lock | `begin_mapping_mutation` | — (the inverse order deadlocks; observed and fixed) |
| Value leaves through the account it entered | `_control_account_for` | `StockMovement.control_account`, immutable with the row |
| An inbound into standing stock keeps that account | `_control_account_for` | — (`inventory_account_reclassification_required`) |
| An empty position holds no account identity | `_save_position` | `stock_balance_empty_position_has_no_control_account` |
| A receipt line states a cost; an issue and a return never do | `add_line` | trigger `inventory_movement_line_frozen_with_document` (INSERT only) |
| A return names exactly one posted issue line | `_validate_return_source` | the same trigger |
| Cumulative returns never exceed their issue | `returnable` under `select_for_update` | — (the issue-line row lock serialises concurrent returns) |
| The final return takes the exact remaining value | `_plan_return` + `apply_inbound(value_in=…)` | — (no residual is left for anybody to chase) |
| An issue with active returns cannot be reversed | `reverse_document` | — |
| A posted document is immutable but for its reversal | status checks | trigger `inventory_movement_document_immutable` (allowlist) |
| Lines freeze with their document | DRAFT-only line services | trigger `inventory_movement_line_frozen_with_document` |
| A number exists exactly from posting, gapless per type and year | `_next_document_number` | `inventory_document_numbered_iff_posted` + partial unique |
| One valuation key per document | duplicate check | `inventory_document_line_valuation_key_unique` (`NULLS NOT DISTINCT`) |

## Deliberate non-invariants, added by Task 1.4

- **`StockMovement.control_account` is nullable, and the null is meaningful.**
  It records a movement posted with no mapping in play at all — the bare
  kernel, exercised by its own tests. Every movement a business document posts
  carries one, held by test rather than by `NOT NULL`, because inventing an
  account for a posting that resolved none would be worse than recording that
  it had none.
- **A storekeeper may enter a receipt cost without holding
  `view_valuation`.** The figure is on the delivery note in their hand. What
  the permission withholds is the ledger's answer — what the organization
  already paid and what the shelf is now worth — not the number they are
  copying from paper.
- **An issue carries one cost centre for the whole document.** Mixed
  destinations need separate documents in Release 1. Per-line centres are a
  later decision, not an oversight.
- **`RETURN_OUT` is absent.** A supplier return must reconcile against a
  supplier invoice, a payable, and a credit note, none of which exist yet; it
  belongs to Procurement in Phase 2. `RETURN_IN` here means unused stock
  coming back from a consumption issue, and nothing else.

## Implemented by Task 1.5, and where each one lives

Transfers, in-transit custody, partial receipts, and shortage closures. See
ADR-020 for the reasoning behind each.

| Rule | Service | Database |
|---|---|---|
| Goods stay on the source branch's books until received | dispatch into the **source** branch's in-transit warehouse | — |
| The two ends are distinct, active, same-organization, non-system | `_validate_transfer_endpoints` | `stock_transfer_warehouses_differ` + trigger `stock_transfer_warehouses_valid` |
| One in-transit warehouse per branch, never user-created | `ensure_in_transit_warehouse` | `warehouse_one_in_transit_per_branch`, `warehouse_in_transit_iff_system` |
| One valuation key per transfer | duplicate check in `add_transfer_line` | `transfer_line_valuation_key_unique` (`NULLS NOT DISTINCT`) |
| Dispatch carries the exact outbound value into transit | `_post_dispatch_effects` with `inbound_value` | — (no gain or loss from movement alone) |
| A receipt takes its own transfer's allocated value | `allocate` + `MovementInput.outbound_value` | — |
| An exact outbound value the position cannot fund is refused | `_require_exact_outbound_is_supported` | — (`allocated_value_exceeds_position_value`) |
| The final receipt or closure takes the exact remainder | `allocate` equality branch | — (no residual for anybody to chase) |
| Nothing may be received or written off that was not dispatched | locked line re-check in `_allocate_receipt` | `transfer_line_remaining_*_within_dispatch` + deferred `transfer_*_line_within_dispatch` |
| Remaining quantity and value empty together | `post_receipt`, `post_shortage` | `transfer_line_remaining_quantity_and_value_agree` |
| Each side of a receipt is dated by its own branch | `post_receipt` two `resolve_business_day` calls | `transfer_receipt_business_date_snapshots_present` |
| Either branch's closed period rolls the whole receipt back | `_period_for` per branch, one transaction | — |
| A cross-branch receipt posts two branch-balanced journals | `_post_receipt_journals` | — (clearing nets to zero for the event) |
| An unmapped role costs nothing: accounts resolve first | `_resolve_receipt_accounts` | — (`account_role_unmapped`, full rollback) |
| A closure needs permission, reason, evidence and a cost centre | `create_shortage`, `CLOSE_TRANSFER_SHORTAGE` | `transfer_shortage_reason_present`, `cost_center_id NOT NULL` |
| At most one active closure per transfer | `_require_transfer_status` | `transfer_shortage_one_active_per_transfer` (partial unique) |
| Dispatch reversal is refused while any child is active | `reverse_dispatch` | — |
| Reversal availability applies at the destination | `reverse_stock_entry` | `stock_balance_quantity_not_negative` |
| Posted transfers, receipts and closures are immutable | status checks | triggers in `inventory/0013` (whole-row allowlists) |
| The aggregate's status is computed, never written | `recompute_transfer_status` | — (transfer allowlist permits status, nothing else) |
| A journalled posting names an account for every dinar | `link_journal_entry` | deferred `stock_entry_accounted_movements_have_accounts` |
| Stock keys are locked canonically across a whole event | `acquire_stock_key_locks`, `acquire_movement_key_locks` | — (ADR-020 §11) |

## Deliberate non-invariants, added by Task 1.5

- **In-transit stock is exempt from the expired-lot rule.** Goods that expire
  on the road still have to be got off it: the receiving branch must be able
  to take delivery of what physically arrived, and a consignment that never
  arrives must be closeable as a shortage. Refusing both would strand the
  value in transit forever with no document able to move it. What arrives is
  still expired, and Task 1.6's waste document writes it off at the
  destination.
- **`StockTransferLine.remaining_quantity` and `remaining_value` are a
  retained cache.** Deriving them on every read would make the §5 allocation a
  race between two concurrent receipts. They are maintained under the
  transfer's row lock, bounded by check constraints, and checked against the
  independently derived figure by reconciliation.
- **A transfer has one source and one destination warehouse.** A consignment
  split across two destinations is two transfers in Release 1.
- **A shortage closure is all-or-nothing.** Partial write-off with an open
  residual is not modelled, per ADR-020 §6.
- **`StockLedgerEntry.journal_entry` is nullable**, for the same reason
  `StockMovement.control_account` is: a posting that never reached the general
  ledger genuinely produced no journal. The invariant is conditional on the
  link existing, which is what makes the bare kernel keep working.

## Task 1.6 — waste, physical counts and manual adjustments

| Invariant | Service | Database |
|---|---|---|
| A reason code's code and application never change | `update_reason_code` takes neither | `inventory_reason_code_identity_is_immutable` |
| An archived code stays reserved forever | archive sets `is_active=False`, never deletes | `inventory_reason_code_unique_per_organization` + delete refused |
| A waste line names a reason of the right application | `_validate_line_reason_code` | `inventory_document_line_reason_matches_type` |
| A reason that demands a comment gets one, at post time too | `_require_waste_line_is_complete` | — (a code may gain the requirement after a draft is written) |
| Waste leaves at the current average, full depletion at zero | `_plan_waste` → `apply_outbound` | `stock_movement_zero_quantity_has_zero_value` |
| Waste needs a cost centre because its account demands one | `require_cost_center_where_the_account_demands_one` | — (class 6 ⇒ `requires_cost_center`) |
| Expired lots leave only through waste, count loss or adjustment | `EXPIRED_LOT_REMOVAL_TYPES` | — (ordinary issue still refused for everyone) |
| A warehouse is frozen **iff** `frozen_by_count` is set | `start_count`, `approve_count`, `cancel_count` | `inventory_warehouse_freeze_owner_is_active` |
| A count may not finish while it still holds a freeze | release-before-status ordering | `inventory_count_releases_its_freeze` |
| At most one active count per warehouse | `start_count` re-check under the lock | `stock_count_one_active_per_warehouse` (partial unique) |
| One count holds at most one warehouse | — | `warehouse_freeze_owner_unique` |
| The in-transit warehouse is never counted | `_require_warehouse_is_countable` | `warehouse_in_transit_is_never_counted` |
| A posting cannot interleave with a freeze | `lock_warehouses_shared` / `_exclusive` | — (advisory, sorted by id) |
| The freeze is read from the database, never from the caller's row | `_require_warehouses_are_not_frozen` | — |
| The book snapshot never changes after the cutoff | nothing writes it | `inventory_stock_count_line_follows_count` |
| The cutoff and its business-date snapshot are frozen at start | — | `inventory_stock_count_is_immutable` |
| A count is numbered from the moment it starts, and only then | `next_document_number` in `start_count` | `stock_count_numbered_iff_started` |
| Counted figures freeze at submission | `record_counts` status check | `inventory_stock_count_line_follows_count` |
| A counted quantity is never negative; null means "not yet counted" | `record_counts` | `stock_count_line_counted_not_negative` |
| An unexpected line has a zero book, not a missing one | `add_unexpected_line` | `stock_count_line_unexpected_has_no_book` |
| One line per `(count, item, lot)`, NULL-safe | `add_unexpected_line` duplicate check | `stock_count_line_key_unique` (`nulls_distinct=False`) |
| The counting sheet carries no book quantity at all | `blind_lines` returns dicts that never held one | — (nothing fetched cannot be leaked) |
| The approver is never the conductor | `approve_count`, command layer | `stock_count_approver_is_not_the_conductor` |
| The book position at approval equals the snapshot | `_require_snapshot_still_matches` | — (`count_snapshot_mismatch`, no silent post) |
| A gain into standing stock uses the standing average | `_resolve_variances` | — (position average unchanged) |
| A gain into an empty position needs an approved unit cost | `_resolve_variances` | — (`approved_unit_cost_required`) |
| A zero unit cost is confirmed, never inferred | `_apply_approved_costs` | `stock_count_line_zero_cost_flag_matches` |
| A count posts its variance and unfreezes atomically | one `@transaction.atomic` | — (failure leaves SUBMITTED and frozen) |
| Count variance is grouped by direction, never netted | `_count_journal_lines` | — |
| A count that moved no value posts no journal | `_post_count_variance` early return | `stock_count_entries_only_when_posted` |
| A cancelled count keeps its snapshot and its history | `cancel_count` sets status | `inventory_stock_count_is_immutable` (delete refused) |
| A cancellation releases exactly its own freeze | ownership re-check under the lock | `inventory_warehouse_freeze_owner_is_active` |
| Reversal does not re-freeze the warehouse | `reverse_count` | — (a corrected figure needs a new count) |
| An active count blocks closing its period | `refuse_close_while_a_count_is_active` | — (`active_inventory_count`) |
| A close and a count start cannot both commit | period row lock in `_period_for(lock=True)` | — |
| A signless movement type must state its direction | `_validate_direction` | — (`direction_required`) |
| A signed movement type must not be given one | `_validate_direction` | — (`direction_not_allowed`) |
| A quantity gain names its cost; nothing else may | `add_adjustment_line` | `inventory_adjustment_line_cost_iff_gain` |
| A revaluation names its amount; nothing else may | `_validate_value_only` | `inventory_adjustment_line_value_iff_value_only` |
| A revaluation moves no quantity | `apply_value_only` | `inventory_adjustment_line_quantity_matches_kind` |
| A revaluation needs standing quantity | `_require_position_can_be_revalued` | — (`value_only_needs_quantity`) |
| A revaluation cannot drive value below zero | `_require_revaluation_stays_positive` | `stock_balance_value_not_negative` |
| Reversing a revaluation cannot drive value below zero | `reverse_stock_entry` value check | `stock_balance_value_not_negative` |
| Posted adjustments and their lines are immutable | status checks | `inventory_adjustment_is_immutable`, `inventory_adjustment_line_follows_document` |

## Deliberate non-invariants, added by Task 1.6

- **A count freezes the whole warehouse, never part of it.** `StockCountScope`
  has one value. A partial count would need a per-key freeze checked on every
  posting; offering it before that exists would be offering a freeze that does
  not hold.
- **`StockBalance.is_frozen` is not written by counts.** It predates Task 1.6
  and remains a separate, finer concept. A warehouse-wide freeze is not an
  item-level one and is not pretended to be.
- **A count with no variance posts no stock entry and no journal**, so
  `StockCount.stock_entry` is null on a legitimately completed count. Nothing
  moved, and an empty posting would make "did this count find anything"
  unanswerable from the ledger.
- **A confirmed-zero gain posts stock and no journal.** Quantity moved and
  money did not; there is genuinely nothing for the general ledger to record.
- **Expired stock may arrive and may sit on the shelf.** Receipt records what
  physically happened; removal is a separate authorized act. Nothing writes it
  off automatically.
- **`owned_freezes` lets one caller post into a frozen warehouse** — the count
  approval that must post the variance into the warehouse it froze. It is not
  a permission, no UI exposes it, and `apps.inventory.counts` is its only
  caller.
