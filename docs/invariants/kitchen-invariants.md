# Kitchen — proposed invariants

The checklist Phase 3 must satisfy. Each line is a test, not a guideline.
Nothing here is optional and none of it may be relaxed to make a suite pass.

These **extend** `docs/invariants/inventory-invariants.md`,
`docs/invariants/procurement-invariants.md` and
`docs/specs/accounting-kernel-invariants.md` rather than replace them. A
production posting that breaks an inventory invariant is broken twice,
because a batch is an inventory posting before it is anything else.

**Status: proposed by Task 3.0 on 2026-08-16.** The "Delivered by" column
names the task that will make each one true; every row is a statement of
intent until that task lands and cites its tests. (Phase 2's file said the
same on its proposal day, and its header records what happened when it was
left saying so too long — this one flips to ENFORCED at the Task 3.11 exit
gate or says why not.)

## The proposed set

| # | Invariant | Enforced where | Delivered by |
|---|---|---|---|
| 1 | A recipe code is canonical uppercase, unique per organization; an archived code stays reserved | `strip().upper()` in the service + `UniqueConstraint`; `on_delete=PROTECT` everywhere | 3.1 |
| 2 | A recipe carries no cost field; every cost is derived or read from a dated snapshot | Model shape + a test asserting the field's absence | 3.1 / 3.3 |
| 3 | A batch recipe's output item is `SEMI_FINISHED` or `FINISHED_GOOD` of the same organization; a portion recipe has no output item | `CheckConstraint` + service validation | 3.1 |
| 4 | Recipe line quantities persist at 6 dp (`CALCULATION_PLACES`), converted to base once at entry, quantized once | `apps/core/quantity.py` boundary discipline (ADR-006) | 3.1 |
| 5 | Approved versions of one recipe never overlap in effective range for one branch | `EXCLUDE USING gist` | 3.2 |
| 6 | Version approval is maker-checker: the approver is never the author | `CheckConstraint` + service | 3.2 |
| 7 | An approved version is immutable, header and lines, except closing its range and supersession | Whole-row allowlist triggers | 3.2 |
| 8 | Version resolution is by date and branch, and historical questions never silently use today's version | One resolver, used by every read; date-boundary tests | 3.2 |
| 9 | Supersession closes the prior version's range in the same transaction that approves the next | Service + test on the range seam | 3.2 |
| 10 | A version's cost equals the sum of its lines' base quantities × as-of averages, quantized once at the money boundary | The derivation is the only implementation; golden-case test | 3.3 |
| 11 | Cost snapshots are append-only and immutable | Insert-only trigger | 3.3 |
| 12 | Only approved versions are produced from, costed, or counted in theoretical consumption | Service guards + queryset filters, tested per surface | 3.2 – 3.8 |
| 13 | A batch is drafted from an approved version of a batch recipe; ad-hoc production does not exist | Service refusal; no code path | 3.4 |
| 14 | A batch names one warehouse; inputs leave it and the output enters it | Model shape + service | 3.4 |
| 15 | A batch posts atomically: every consumption, the output, the number, the audit event, and the journal where one exists — or nothing | One transaction through the inventory kernel | 3.5 |
| 16 | **Value is conserved**: output inbound value equals the sum of consumed values, to the fils | `inbound_value` channel + `verify_kitchen` | 3.5 |
| 17 | Yield loss posts nothing; it is absorbed into output unit cost and reported | No variance journal exists; yield report | 3.5 / 3.6 |
| 18 | The batch journal is the per-account net of its movements; zero-net batches post no journal; non-zero nets agree with `verify_inventory_against_gl` by construction | Posting service + verifier | 3.5 |
| 19 | A produced lot records its producing batch (`produced_by_*`) and expires by the item's shelf life from the batch's business date | Posting writes the reserved fields | 3.5 |
| 20 | Expired ingredients cannot enter a batch | `PRODUCTION_OUT` stays out of the expired-removal set (kernel, already enforced) | 3.5 |
| 21 | A posted batch is immutable except reversal; reversal mirrors values exactly, checks availability, happens once, with a reason | Triggers + kernel reversal | 3.5 |
| 22 | One journal per `(organization, KITCHEN_PRODUCTION_BATCH, public_id, POSTED)`; idempotency keys unique per organization with fingerprint | ADR-017 mechanics, unchanged | 3.5 |
| 23 | Negative stock is refused in production with no bypass | Kernel `_require_available`, already enforced; race test | 3.5 |
| 24 | A meal record moves no stock and posts no journal; it resolves and freezes its version at record time | Model shape + tests | 3.7 |
| 25 | Meal corrections are cancellation-and-re-record, never edits; variance reads only `RECORDED` rows | Status machine + queryset filters | 3.7 |
| 26 | Actual consumption equals the charter's formula over posted movements for the selected warehouse — a derivation, never a stored number | Report query = the formula; golden case | 3.8 |
| 27 | Theoretical consumption uses the version effective at each record's date, never today's | Resolver reuse + date-boundary test | 3.8 |
| 28 | The variance report labels its theoretical coverage (sales absent until Phase 4) | Screen + CSV assertion | 3.8 |
| 29 | `verify_kitchen` proves batch agreement, value conservation, and source identity, and reports without repairing | The verifier + planted-defect tests | 3.9 |
| 30 | Every report obeys the Phase 1 contract; recipe cost columns are omitted, not blanked, without `view_recipe_cost` | Inherited report machinery + per-report tests | 3.1 – 3.9 |

## Rules that carry over unchanged

Not restated as kitchen invariants, because they already hold and the kitchen
must not weaken them: posted journal immutability on every column; the
balanced-entry trigger at COMMIT; idempotency keys per organization matched
against fingerprints; permission **plus** scope, 404 out of scope, 403
without authority; Decimal only, money and quantity utilities never shared;
resolve-with-the-caller, never fetch-then-check; period validation on every
posting; audit `previous_state` re-read from the database.

## Deliberate non-invariants

- **"A batch must match its recipe's quantities."** It must not. The batch
  records what the kitchen actually used; the difference is the variance
  report's subject matter. Refusing mismatches would teach kitchens to
  falsify the record.
- **"Production must post a yield variance."** There is no approved standard
  cost to hold a variance against; loss is absorbed into unit cost and
  reported (RCP-035, proposed ADR-025).
- **"Every batch posts a journal."** A batch whose accounts all net to zero
  has nothing to tell the GL, and the kernel refuses zero-value lines. The
  stock ledger carries the event's identity either way.
- **"A staff meal consumes stock."** Its ingredients already left stock
  through kitchen issues and batches; the meal record is the explanation,
  not a second consumption (RCP-043).
- **"Recipes are hidden from cooks."** The card and quantities are the job;
  only cost columns are gated, omitted not blanked.
