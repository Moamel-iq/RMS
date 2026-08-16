# Kitchen — proposed invariants

The checklist Phase 3 must satisfy. Each line is a test, not a guideline.
Nothing here is optional and none of it may be relaxed to make a suite pass.

These **extend** `docs/invariants/inventory-invariants.md`,
`docs/invariants/procurement-invariants.md` and
`docs/specs/accounting-kernel-invariants.md` rather than replace them. A
production posting that breaks an inventory invariant is broken twice,
because a batch is an inventory posting before it is anything else.

**Status: proposed by Task 3.0 on 2026-08-16, extended by Task 3.0A and again by
Task 3.0B, both on 2026-08-16.** The "Delivered by" column names the task that
will make each one true; every row is a statement of intent until that task
lands and cites its tests. (Phase 2's file said the same on its proposal day,
and its header records what happened when it was left saying so too long — this
one flips to ENFORCED at the Task 3.11 exit gate or says why not.)

Invariants 1 – 30 come from Task 3.0. **Invariants 31 – 46 were added by Task
3.0A**, and every one of them exists because reading `KM-RCP-004` or checking
the charter's consumption formula against the actual document set turned up
something the first pass had missed. **Invariants 47 – 52 were added by Task
3.0B**, after the owner supplied the Arabic recipe book and the two plate-card
decks: each one guards a fact those documents established, because the risk
changes once real figures exist. Before, the danger was inventing a number.
Now it is losing track of where a real one came from.

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

### Added by Task 3.0A

| # | Invariant | Enforced where | Delivered by |
|---|---|---|---|
| 31 | A version's method is structured steps; `sequence` is unique per version; approved steps are immutable with the version | `UniqueConstraint` + the version's allowlist trigger | 3.1 / 3.2 |
| 32 | A step's ingredient share is `0 < share ≤ 1`, and the shares of one line across its steps never exceed 1 | `CheckConstraint` per row + service for the per-line sum | 3.1 |
| 33 | **No step affects any cost, consumption, theoretical quantity or posting** | The costing and consumption services never read the step tables; tests assert identical results with and without steps | 3.1 – 3.9 |
| 34 | `expected_duration` and `temperature_c` are null unless a source supplied them | No default, no inference; a test asserts the demo seed leaves them null | 3.1 |
| 35 | A recipe **with** an output item is referenceable only as a `RecipeLine`; a recipe **without** one, only as a `RecipeComponent` | `kitchen_recipe_component_follows_its_version` + `validate_component_edge` | `apps/kitchen/tests/test_components.py::TestTheTwoShapes` | 3.2B |
| 36 | No component cycle exists at any depth, and nesting never exceeds the approved limit | `recipe_component_recipe_is_not_its_own_parent` + `cycle_path` under the graph lock | `apps/kitchen/tests/test_components.py::TestCycles` | 3.2B |
| 37 | A component's child version is effective on the parent's start date, for every branch the parent applies to | `require_effective_coverage` at activation | `apps/kitchen/tests/test_component_coverage.py::TestCoverageAtActivation` | 3.2B |
| 38 | Component cost rolls up recursively and is quantized **once**, at the top | One derivation used by every read; golden-case test against a hand-computed three-level tree | 3.3 |
| 39 | A non-stocked component creates no item, no stock and no movement; flattened batch lines carry `source_component_version` and `component_path` | Flattening service + posting tests | 3.4 |
| 40 | Exactly one primary serving per version; a serving's unit is convertible to the output basis | Partial unique index + `apps/units` dimension check at entry | 3.1 |
| 41 | Serving cost allocation sums **exactly** to the batch cost, and the rounding policy never moves money | `apps/core/allocation.allocate` is the only implementation; awkward-split test | 3.3 |
| 42 | No dish, cut, serving code or gram figure appears anywhere in `apps/kitchen` source | A convention test over the app's Python files | 3.1 – 3.9 |
| 43 | **Every posted movement at a warehouse falls in exactly one consumption bucket**, and the resulting stock identity reconciles to the Phase 1 balance | The report is a partition by construction; `verify_kitchen` asserts the identity | 3.8 / 3.9 |
| 44 | Attributed link quantity never exceeds the source line, and a link never mutates an inventory document | Service under `select_for_update` + `verify_kitchen`; inventory has no import of kitchen | 3.8 |
| 45 | A batch spans one business date and one warehouse; no partial or multi-day production is representable, and an attempt is **refused**, never approximated | No `IN_PROGRESS` state exists; named service refusals with tests | 3.5 |
| 46 | A posted batch with no journal has **provably** zero per-account nets | `verify_kitchen` recomputes the nets for every journal-less batch | 3.5 / 3.9 |

### Added by Task 3.0B

The recipe book and plate cards (§0 S-3, S-14, S-15) turned three open questions
into sourced facts, and every one of them needs an invariant, because a sourced
fact that nothing enforces decays back into a guess.

| # | Invariant | Enforced where | Delivered by |
|---|---|---|---|
| 47 | Every row imported from a source document carries `source_document` **and** `source_page`; both are null only for hand-entered rows, never partially set | `CheckConstraint` (`both null or both set`) + importer validation | 3.1 / 3.10 |
| 48 | Every quantity carries a `measurement_basis`, and **no report sums or compares quantities across different bases** | Non-null field + a query-service guard with a named error; golden test that a raw and a cooked quantity refuse to aggregate | 3.1 / 3.8 |
| 49 | Where sources disagree, **both claims persist**; the importer never picks a winner | Conflict rows survive validation and surface on a report; a test plants two conflicting source rows and asserts both land | 3.10 |
| 50 | Imported versions and steps land **`DRAFT`**; no import path can set `APPROVED`, and approval remains a distinct human act | Importer writes the draft status only; a test asserts no import can produce an approved version | 3.10 |
| 51 | **No code derives one sellable plate's quantity, cost or price from another's** — no doubling a half, no halving a whole | Convention test over `apps/kitchen`; the plate's own version is the only source; the physical serving factor applies to the output, never to the plate | 3.1 / 3.3 |
| 52 | **No PDF is a runtime dependency**: nothing in `apps/kitchen` opens, parses, stores or links a PDF at request time, and none is tracked in Git | Convention test for PDF handling in the app; repository check that no `*.pdf` is tracked | 3.1 – 3.11 |

### Enforced by Task 3.1

The first invariants in this file to stop being proposals. Each names the test
that holds it, so a later reader can tell an enforced rule from an intended one.

| # | Enforced by |
|---|---|
| 1 | `recipe_code_unique_per_organization` + `apps/kitchen/tests/test_recipe_master.py::TestCodeIsIdentity` |
| 2 | Absence of any cost field + `::TestNoMoneyAnywhere` |
| 3 | `recipe_output_item_matches_type` + `::TestTheOutputItemRule` |
| 4 | `CALCULATION_PLACES` on every quantity + `test_draft_structure.py::TestLines` |
| 30 | Cost columns do not exist yet — Task 3.3 adds them, and `::TestScreens` asserts none is rendered now |
| 40 | `recipe_serving_one_primary_per_version` + `::TestServings` |
| 42 | `::TestServings::test_no_dish_or_gram_figure_is_hard_coded_in_the_app` |
| 47 | `recipe_*_provenance_is_complete` on all six tables + `::TestProvenance` |
| 48 | `MeasurementBasis` non-null + `::TestLines` |
| 50 | `recipe_version_task_3_1_draft_only` + `::TestTheLifecycleBoundary` — **the constraint was replaced by Task 3.2A**: an import still cannot produce an approved version, because approval is now four signatures and an explicit command rather than a status assignment, and `::TestTheLifecycleBoundary::test_the_database_refuses_a_status_jump` holds it |
| 52 | `::TestNoImportsFromTheProprietarySources` |

Invariants 5 – 9 and 12 (approval, effective dating, supersession, immutability)
are **not** enforced here and cannot be: Task 3.1 has no approval lifecycle to
enforce them against. They belong to Task 3.2, and saying so is more useful than
a partial trigger that would look like protection.

### Enforced by Task 3.2A

The five Task 3.1 could not reach, plus the four the lifecycle itself created.
Each names the mechanism *and* the test, so a later reader can tell an enforced
rule from an intended one.

| # | Enforced by |
|---|---|
| 5 | `recipe_scope_no_overlapping_ranges` (`EXCLUDE USING gist`, migration `0005`) + `test_effective_dating.py::TestOverlapEnforcement` and `test_version_concurrency.py::TestTwoActivationsCannotOverlap` at real COMMIT |
| 6 | `recipe_version_approver_is_not_the_author`, `..._is_not_the_submitter` + `approve_recipe_version`'s three actor checks + `test_version_lifecycle.py::TestApproval` |
| 7 | `kitchen_recipe_version_is_immutable` (whole-row allowlist, five permitted transitions) and the five child-table triggers + `test_version_immutability.py::TestTheDatabaseRefusesRawWrites` |
| 8 | `resolve_recipe_version` with a **required** `on_date` + `test_effective_dating.py::TestRangeBoundaries` covering the day before, `effective_from`, the final included day, the day after, and the open-ended case |
| 9 | `_supersede_locked` inside `activate_recipe_version`'s transaction + `test_version_lifecycle.py::TestSupersession::test_supersession_closes_the_predecessor_at_the_seam` |
| 12 | `RESOLVABLE_VERSION_STATUSES` excludes `APPROVED` + `test_effective_dating.py::TestStatusIsNotResolution` |

### Added by Task 3.2A

Four rules the lifecycle created, each because building it turned up a way to
get the boundary wrong that nothing before had needed to forbid.

| # | Invariant | Enforced where | Delivered by |
|---|---|---|---|
| 53 | Approval and effect are **two** decisions: an `APPROVED` version resolves for no date, and activation is a separate command behind a separate permission | `RESOLVABLE_VERSION_STATUSES` + `activate_recipe_version` + `::TestApproval::test_approval_does_not_make_a_version_effective` | 3.2A |
| 54 | Organization-wide effective scope is **materialised** per branch; no row means "everywhere" anywhere the overlap constraint has to see it | `RecipeVersionBranchScope` + `kitchen_scope_follows_its_version` + `::TestOverlapEnforcement::test_an_organization_wide_claim_collides_with_a_branch_claim` | 3.2A |
| 55 | The effective range is inclusive at both ends, expressed once, and the supersession seam has no gap and no overlap | `daterange(..., '[]')` + `lifecycle.covers_on_date` + `::TestRangeBoundaries::test_the_final_included_day_resolves` | 3.2A |
| 56 | `DEMO_FICTIONAL` approval evidence exists only inside the `DEMO-` namespace, **and a demo recipe cannot claim a signed form** | `kitchen_version_evidence_matches_namespace` + `kitchen_review_is_append_only` + `::TestDemoDataset::test_every_demo_approval_is_evidenced_as_fiction` | 3.2A |
| 57 | A component names one **exact** child version and no command re-points it; adopting a newer child is a new parent version | `RecipeComponent.component_version` PROTECT + no re-point path anywhere | `apps/kitchen/tests/test_components.py::TestExactVersionAdoption` | 3.2B |
| 58 | At **initial activation** a parent's child is effective on the parent's start date at every applicable branch; afterwards the exact reference is frozen and survives the child's supersession | `require_effective_coverage` | `apps/kitchen/tests/test_component_coverage.py` | 3.2B |
| 59 | A component may be inserted, changed or deleted only while its parent is `DRAFT`, as a **whole row** with an empty allowlist | `kitchen_recipe_component_follows_its_version` | `apps/kitchen/tests/test_components.py::TestTheDatabaseRefusesRawWrites` | 3.2B |

Invariants 35 – 37 (the component mutual exclusion, cycle and depth limits, and
child-range compatibility) remain **proposals**. They are Task 3.2B's, and
`RecipeComponent` does not exist — a test asserts its absence rather than a
comment claiming it.

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
- **"A half costs half."** True of the **bird**, false of the **plate**, and
  Task 3.0B has the source for both halves of that sentence. The recipe book
  halves chickens exactly (*"والدجاج الى نصفين"*, p1), so the physical serving
  factor is 0.500 and the meat cost really does divide by two. The plate cards
  then show the whole مندي plate carrying **1,300 g** of rice against the
  half's 700 g with doubled sides, and the workbook prices the two at 25,000
  and 13,000. So `cost(whole plate) ≠ 2 × cost(half plate)`, and invariant 51
  forbids any code from pretending otherwise (RCP-123, RCP-124).
- **"Every movement in a kitchen warehouse is consumption."** A transfer that
  brings rice into the kitchen changed custody, not state. Counting it
  alongside the production issue that later consumes the same rice is the
  double count RCP-098 – RCP-103 exist to prevent, and it would put a
  permanent unexplainable overage into the variance report.
- **"Waste is waste."** Wasting raw onions and wasting cooked rice are
  different losses: the second one's ingredients already left stock through the
  batch that cooked them, and adding it to ingredient consumption charges them
  twice (RCP-105).
