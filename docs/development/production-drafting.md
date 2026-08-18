# Production drafting

> **Task 3.5 has shipped.** Everything below is still true of a *draft*; what
> happens when one commits is `production-posting.md`. Where this document says
> a thing does not exist yet — a post button, a lot picker, a value — read it as
> "does not exist on a draft", because that is now the accurate claim.

Task 3.4. How a kitchen says what it intends to cook, and what it actually
consumed — before anything moves.

This document is for somebody about to change `apps/kitchen/production.py`,
`production_views.py`, `production_reconciliation.py` or migrations 0010–0016.
It records the decisions that are not obvious from the code and the two defects
that were only found by asking the database to check.

---

## 1. What a production draft is, and is not

A `ProductionBatch` is one intended production run, drafted from **one exact
`RecipeVersion`**, for one branch, one warehouse and one planned business date.

It **is**:

- a plan — flattened requirements with exact component paths and scaled
  quantities;
- a record of reality — what was actually consumed, of what, and how much came
  out;
- editable, in the ways an operator legitimately edits a draft.

It is **not**:

- a posting. Nothing here moves stock, writes a journal, draws a document
  number, creates a lot, reserves anything or reads a balance.

The second half is a **check constraint**, not a promise:

```
production_batch_is_draft_only_until_task_3_5
```

named after the task that must delete it, so nobody removes it while tidying.

---

## 2. The three tables, and why there are three

| Table | Holds | Mutability |
|---|---|---|
| `ProductionBatch` | the decision and the output | decision frozen; scale revisable; output editable |
| `ProductionBatchLine` | one flattened requirement, one economic path | source frozen; planned quantity rewritten only by a rescale |
| `ProductionBatchActualLine` | what was consumed against that requirement | editable while the batch is a draft |

The domain spec sketched `consumed_quantity` as a **column on the line**. That
shape cannot express a partial substitution — 3 kg of the planned item plus 1 kg
of an approved stand-in is two facts about one requirement — so it is normalized
into the third table. The departure is recorded in the spec's section 28.2.

Keeping the plan and reality on different rows is RCP-030 made structural: an
operator who consumed something else has recorded a fact, not amended a recipe,
and there is no way to edit the requirement to make the disagreement disappear.

---

## 3. The version is resolved once

Creation names a recipe, a branch and an **explicit** planned business date, and
`resolve_recipe_version` answers which structure governed that branch on that
day. The answer is stored and **never re-resolved** — not when a newer version
activates, not when a child is superseded, not when the batch is reopened next
week.

A wrong date is corrected by discarding the draft and drafting again. Re-pointing
an existing batch would change what a half-finished document claims the kitchen
is making.

There are exactly **two** call sites of `resolve_recipe_version` in
`production.py` — creation, and the preview that must agree with it — and a
source test pins that count. A third would be the silent re-resolution the whole
design forbids.

---

## 4. One expansion engine

`apps/kitchen/expansion.py` holds the only walk of the component graph. Task 3.3
was moved onto it during Task 3.4, so a cost card and a requirement list cannot
disagree about what a recipe contains.

An AST test asserts that neither `costing.py` nor `production.py` defines a
recursive function or touches `RecipeComponent.objects`, and that both import
`expand_recipe_version`.

`graph.component_tree` still walks the graph, and that is deliberate: the
**display** tree needs the nesting the engine flattens away. It is pinned against
the engine by a test rather than described as "the only other one" and forgotten.

---

## 5. Scaling, and the two defects that surfaced

Three figures are three views of one decision:

```
ProductionBatch.multiplier
ProductionBatch.expected_output_quantity
ProductionBatchLine.planned_base_quantity     (one per requirement)
```

The multiplier is **revisable** while a batch is a draft — how much to make is
exactly what changes before cooking — so migration 0011 deliberately leaves it
out of the frozen decision. Migration **0015** is what keeps revisable from
becoming independently mutable: a deferred constraint trigger checks all three
against each other at COMMIT.

Deferred, because a legitimate rescale updates the header and every requirement
and is inconsistent *by construction* in between. `SET CONSTRAINTS ALL IMMEDIATE`
is how the tests observe it.

Writing that trigger surfaced two real defects, neither visible by reading:

1. **The multiplier was quantized before storage but not before use.**
   `2.5000005` stored `2.500001` and produced an expected output computed from a
   figure the row does not contain.
2. **The cumulative multiplier was used at walk precision and stored at twelve
   places.** Creation scaled by the unrounded product; a later rescale scaled by
   the stored one. Rescaling a two-level recipe to the multiplier it already had
   moved its planned quantities.

Both are closed by having **one** definition of each product —
`scaled_line_quantity` and `scaled_expected_output` — used by creation, rescale
and the preview, and mirrored by the trigger in SQL. Both carry eighty digits of
context so the Python and the `numeric` arithmetic are exact and cannot disagree
about a rounding.

---

## 6. Cross-dimensional consumption

RCP-022 approves **items**, never conversions. A kitchen may legitimately accept
a stand-in that nothing converts to.

So a requirement can have consumption in two dimensions, and there is no honest
way to add them. The rule, held through the model, services, screens, API and
verifier:

- every actual row is shown and reported **separately**;
- a variance is a number **only** where the dimensions agree —
  `comparable_consumption` sums same-dimension rows and returns `None` otherwise;
- where they do not, the answer is the sentence *not quantitatively comparable*,
  never a blank and never a total;
- **no physical conversion ratio is invented, at any layer**;
- Task 3.5 values each row separately, which is where the two become comparable
  again — in money, which they share.

`None` rather than zero, because zero is a claim about the kitchen and `None` is
a claim about the arithmetic.

Readiness reports this as an **observation**, not a problem, and
`verify_production_drafts` does the same: a legitimate act must not make a
correct database exit non-zero forever. A red list nobody can clear stops being
read within a week, and the real defects go unread with it.

---

## 7. The lock order

```
Recipe → exact RecipeVersion → ProductionBatch
→ ProductionBatchLine        by (line_order)
→ ProductionBatchActualLine  by (line_order, entry_order)
```

**The batch comes first even when the command names a single row.** Editing one
actual quantity looks like a reason to lock that row alone; doing so inverts the
order against `rescale_production_batch` and deadlocks under the ordinary case of
one operator rescaling while another types a quantity. `_lock_actual_row` and
`_lock_requirement` exist so no command has to remember.

The batch lock is also what makes actual-row mutations **serialized**: with it
held, "is a row left to say what was consumed?" has a stable answer, which is why
two simultaneous removals cannot both succeed.

**No inventory stock-key lock is taken, deliberately.** Task 3.4 moves nothing,
so locking stock to make a draft "safe" would let a drafting screen block a
delivery.

---

## 8. Readiness

Derived, never stored. There is no `READY` status and no readiness column: a
stored flag goes stale the moment somebody edits a quantity, and the only way to
trust it would be to recompute it.

- **Every problem at once.** An operator fixing one thing at a time and
  resubmitting is an operator the system is wasting.
- **No stock query.** Availability, lots, expiry, locations and negative-stock
  refusal are Task 3.5's, at posting. A test records the SQL readiness runs and
  asserts no inventory table appears in it.
- **Problems and observations are separate lists**, for the reason in §6.

---

## 9. The verifier

`verify_production_drafts` reports and never repairs. There is no `--repair`, and
the frozen columns refuse an `UPDATE` at the database anyway.

A draft that disagrees with itself is evidence that something wrote it wrongly or
reached behind a trigger. Smoothing it over would erase the evidence that the
question existed. The answer is to discard the draft and draft again — one
command, and it leaves an audit trail.

Exit 1 on **defects**; observations are reported and do not affect the status.
Exit 2 for an organization code that names nothing, because "checked an
organization that does not exist" must never look like "clean".

---

## 10. What Task 3.5 inherits

- The constraint and triggers to remove, deliberately and by name (spec §28.1).
- Requirements already flattened, path-stamped and scaled — posting has no
  expansion left to do.
- Actual rows already carrying a complete conversion snapshot, so the posting
  quantity needs no re-derivation.
- Readiness, which reports every blocker but checks no stock.
- One rule to respect rather than resolve: a cross-dimension substitution is
  valued row by row, and there is no ratio between kilograms and litres to find.

---

## See also

- `docs/tasks/task-3-0-recipes-production-domain-spec.md` §28 — as built, and
  every departure from the sketch
- `docs/invariants/kitchen-invariants.md` — invariants 73–90
- `docs/development/recipe-costing.md` — the other consumer of the shared
  expansion engine
- `docs/decisions/ADR-024-recipe-versioning-and-the-effective-dated-cost-basis.md`
