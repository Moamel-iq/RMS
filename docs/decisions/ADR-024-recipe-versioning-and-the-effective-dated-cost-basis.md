# ADR-024 — Recipe structure, versioning and the effective-dated cost basis

- **Status:** **Accepted** (Task 3.2A, 2026-08-16). Proposed by Task 3.0 and
  extended by Tasks 3.0A and 3.0B; written by the task that first implements
  its subject, as the decisions index requires. The half of the scope that
  concerns **nested recipes** — `RecipeComponent`, the mutual-exclusion
  constraint, cycle and depth validation — is deliberately **not** accepted
  here and is implemented by Task 3.2B.
- **Date:** 2026-08-16
- **Related:** ADR-006 (decimal and rounding), ADR-007 (organization and branch
  boundaries), ADR-008 (business date), ADR-016 (permission and scope),
  ADR-018 (stock ledger — where the cost eventually comes from)
- **Detail:** `docs/tasks/task-3-0-recipes-production-domain-spec.md` §4, §5,
  §5A, §5C, §13, §22.1, §25

A recipe is a claim about money that nobody has spent yet. This decision
settles what makes such a claim trustworthy, and what has to be true before a
July report is allowed to answer with a September recipe.

## Context

The architecture charter states the requirement verbatim and absolutely:

> Historical sales must use the recipe version that was effective when the item
> was sold. A recipe changed in September must not silently change the
> theoretical cost of July sales.

That single sentence rules out the two obvious designs. **Editing a recipe in
place** restates every historical read the moment somebody corrects a quantity.
**Keeping versions but resolving "the latest one"** is the same defect wearing a
version number: it answers September's question correctly and July's question
with September's recipe.

The kitchen's own paperwork already reaches for the same controls and does not
quite manage them. `KM-RCP-004` — *نموذج اعتماد مكونات وكلفة الأصناف* — carries
`يعتمد من تاريخ` (approved from date) and `آخر مراجعة` (last review) on every
costing card, and assigns `الكمية المعتمدة` to *"الشيف + المحاسب + المدير"*:
chef **plus** accountant **plus** manager. Its signature page carries a fourth
line for the store. So maker-checker and effective dating are not ERP
conventions being imposed on this kitchen; they are the kitchen's existing
controls, written down and made enforceable.

The form is also, today, entirely blank. That is what makes it useful: it is
**authoritative about shape and silent about numbers**, and this design takes
its shape and refuses its numbers (RCP-058, RCP-059).

## Decision

### 1. A version has a six-state lifecycle, and one terminal state

```
DRAFT ──submit──► SUBMITTED ──approve──► APPROVED ──activate──► ACTIVE
                      │                                            │
                      └──reject──► REJECTED                        └──supersede──► SUPERSEDED
```

`DRAFT` is mutable and freely discarded. `SUBMITTED` freezes the structure so
every reviewer reads the same document — reviewing something the author is
still editing proves nothing about what eventually gets approved. `APPROVED`
records that the control is satisfied. `ACTIVE` is a claim on dates and
branches. `REJECTED` keeps the refusal, its actor and its reason. `SUPERSEDED`
is closed by a named replacement and **stays resolvable for its own dates**.

**There is no `EXPIRED` state**, and Task 3.0 §4's single terminal state is why.
Expiry is not a state a row sits in; it is a fact about a date. A version whose
`effective_to` has passed simply does not cover today, which the range already
answers. Storing it would need a clock-driven job, and on every morning that job
did not run, the stored status and the stored range would disagree about the
same version. `RecipeVersion.is_expired_on()` derives it instead.

**There is no `DISCARDED` state either.** Discarding a draft deletes the row —
nothing outside a draft may reference one — and the audit event carries the
identity forward. A status nothing ever reaches would be a lie in an enum.

### 2. Approval is four signatures, and the fourth is a different person

Three review rows must exist and be positive before final approval is offered:
the kitchen's, the store's, and the accountant's costing-evidence review. The
final approval writes a fourth row and moves the version.

The separations enforced are the ones the source describes, and no more:

- the approver is never the author (`CheckConstraint` + service, RCP-013);
- the approver is never the submitter;
- the approver is never any of the three reviewers — the checker checks, they
  do not also make;
- the kitchen review and the costing review are given by **two different
  people**, because "الشيف + المحاسب" names two parties.

The store's review is deliberately *not* forced apart from the kitchen's. In a
small branch the person who knows the cut is the person who knows the sack, and
inventing a separation the source does not claim would be as wrong as dropping
one it does.

**No global `CHEF` role is created.** The four are *responsibilities exercised
on one document*, not posts in the organization chart, so they are review types
carried by whichever role holds `review_recipe_version`. Adding a role to the
global access model of the whole ERP to record one column of one kitchen form
would be the tail wagging the dog.

### 3. Approval and effect are two decisions, with two permissions

Agreeing that a recipe is correct, and deciding that it governs Sunday's
costing, are different decisions and the second one moves money. `APPROVED`
therefore resolves for **no** date; `activate_recipe_version` is a separate
command behind `activate_recipe_version` permission, and it is where the
effective range and the branch set are supplied.

This also answers the owner's KD-02 amendment cleanly: a real branch recipe may
be *captured* as a draft and may not be *approved or activated* until its
`KM-RCP-004` evidence is complete. The gate sits at approval, which is exactly
where the owner moved it.

### 4. The effective range is inclusive at both ends

`[effective_from, effective_to]`, with a null upper bound meaning open-ended.
This is the repository's standing convention — `ItemPackageConversion` has used
`daterange(effective_from, effective_to, '[]')` since Task 1.0 — and RCP-016
depends on it: supersession closes the predecessor **the day before** the
replacement begins, which is a seam with no gap and no overlap only if the
upper bound is included.

The convention is expressed once, in `lifecycle.covers_on_date()`, and the
database constraint, the services, the resolver, the API and the screens all
read it through that function or through the identical `'[]'` daterange. A
one-day hole is the classic way this rule rots, and it is invisible until a
report for a version's final day comes back empty.

### 5. Organization-wide scope is **materialised**, never implied

`Recipe.branches` keeps Task 3.1's convention — no rows means every branch —
because that is a statement about where a *dish* is cooked, and no constraint
depends on it.

The **effective scope of a version** cannot work that way. A row claiming *all
branches* and a row claiming *branch B* overlap, and no database constraint can
see that, because there is nothing to compare branch B against. So an
organization-wide activation writes **one `RecipeVersionBranchScope` row per
applicable branch**, and records that it did so in `is_organization_wide` — a
fact about provenance, not a modifier of scope.

After that, "do these two claims collide" is a question about two ordinary rows,
and `EXCLUDE USING gist (recipe, branch, daterange)` answers it — the mechanism
invariant 7 already uses for package conversions. Ambiguity becomes
unrepresentable rather than merely unlikely.

The constraint is `DEFERRABLE INITIALLY IMMEDIATE`. In normal use that is
identical to an immediate constraint; it exists so a test can defer it inside a
transaction it then rolls back, activate a genuinely colliding version through
the real service, and prove that the resolver's ambiguity branch and the
verifier's ambiguity finding actually work. *"Cannot happen"* and *"is not
handled"* are different claims, and only one of them survives a migration that
was reverted on one machine.

### 6. One resolver, and it will not guess

```python
resolve_recipe_version(*, recipe, branch, on_date) -> RecipeVersion
```

`on_date` has no default. A posting-facing read that quietly meant *today* would
give the right answer for the whole of development and the wrong one the first
time somebody re-ran a July report in September — which is the exact failure the
charter's sentence forbids.

It returns `ACTIVE` **or** `SUPERSEDED` versions, because a superseded version is
still the truth about its own days. It raises
`recipe_version_not_effective` when nothing covers the date and
`recipe_version_ambiguous` when more than one does, and it never falls back to
`latest()`, the highest version number, the most recently updated row, or "the
active one".

### 7. A frozen version is frozen as a whole row

Once a version leaves `DRAFT`, the header and **every owned child row** —
lines, substitutes, steps, step links, servings, effective scopes and reviews —
refuse insert, update and delete at the database.

The header uses the allowlist idiom migration `accounting/0005` established:
each permitted transition builds the row it expects to see and compares whole
rows, so a column added to this table next year is protected before anybody
remembers to protect it. A blocklist has to be remembered; an allowlist cannot
be forgotten. `accounting/0005` records what forgetting one cost.

Correction after approval is: old immutable version → new version number → new
review → new effective scope → explicit supersession. There is no path back to
`DRAFT`, and no service, form, API route or raw statement provides one.

### 8. Reviews are append-only signatures

One row per `(version, review_type)`, never updated, never deleted. A reviewer
who has changed their mind does not overwrite a signature; the version is
rejected and the next one is prepared. A rejected review does not by itself move
the version — it makes final approval refuse, and somebody with
`reject_recipe_version` still has to close it. Recording a doubt and ending a
version are two acts, and one person may hold only the first.

### 9. Demo evidence is visibly demo, in both directions

`ApprovalEvidenceKind` is `SIGNED_FORM` or `DEMO_FICTIONAL`, and a trigger
permits `DEMO_FICTIONAL` only inside the `DEMO-` namespace **and refuses
`SIGNED_FORM` inside it**. The second direction is the one that matters: a demo
screenshot carrying what looks like a signed `KM-RCP-004` is precisely how
unapproved figures acquire authority (RCP-126).

### 10. Nothing here posts

Approving a recipe is an agreement about a document. No stock movement, no
balance, no journal entry, no cost — proved by counting rows before and after
every command rather than by asserting it. The production batch of Task 3.5 is
the event that moves value (RCP-002).

## Alternatives considered

**A `valid_from` column and "the row with the highest one that is ≤ today".**
Rejected: it is the resolver defect with extra steps, and it cannot express two
branches on two different versions at all.

**An `is_current` flag maintained by the service.** Rejected: a flag is a cache
of a date comparison, and the one that drifts is always the cache. It also
cannot answer a historical question, which is the only question that matters.

**A nullable `branch` on the scope row, null meaning everywhere.** Rejected in
§5: it reads well and makes overlap unenforceable, which trades a real guarantee
for a shorter table.

**Storing `EXPIRED`.** Rejected in §1: a status that depends on a clock is wrong
on every day the clock-driven job does not run.

**A generic ERP-wide approval engine.** Rejected as scope: the kitchen's control
is a specific four-signature instrument that already exists on paper, and
generalising it before a second module needs it would fit neither.

## Consequences

- A recipe correction costs a whole version, four signatures and an explicit
  activation. That is the intended weight: the alternative is a quantity change
  that silently restates every meal already costed.
- Two versions of one recipe can never be in flight at once
  (`recipe_version_one_open_per_recipe`), so a second draft has to wait for the
  first to be approved or rejected. Refusing early is kinder than refusing after
  every reviewer has signed.
- A branch created after a version was activated has no scope row, and the
  recipe does not apply there until somebody activates a version for it. This is
  deliberate — a new branch inheriting an approved costing basis nobody chose for
  it would be worse — and it is the one place an operator must act that the old
  "empty means everywhere" convention would have hidden.
- Task 3.3 can derive cost against an exact version for an exact date, which is
  what makes a dated cost snapshot meaningful.
- Task 3.2B adds `RecipeComponent` **inside** this boundary: the child reference
  is to an exact approved version, and the freeze extends to it by the same
  trigger family.

## Settled by this ADR

- Lifecycle vocabulary and the absence of `EXPIRED` and `DISCARDED`.
- The four-party evidence model and which separations are enforced.
- Approval and activation as two decisions with two permissions.
- The inclusive range convention and the supersession seam.
- Materialised organization-wide scope, and the exclusion constraint over it.
- One resolver, with a required business date and two stable error codes.
- Whole-row immutability across every owned table.
- Demo evidence namespacing, in both directions.

## Still open

- ~~**Costing itself**~~ — **settled by Task 3.3 (2026-08-17)**; see the
  extension below. Nothing in the *original* decision stores or derives money,
  and that is still true: costing is derived, and the only rows it persists are
  append-only snapshots.
- **KD-19 and KD-20 data** — the sauce unit gap and the undocumented appetizer
  blends remain open as *data*, exactly as the register recorded. The unit layer
  refuses the conversion rather than guessing it, and this ADR does not change
  that.


## Extended by Task 3.2B — the nested recipe graph

Task 3.2B implemented Task 3.0 §5B. Nothing above is revised; three decisions
are added, and all three are about the same thing this ADR has been about from
the start — *what a version is allowed to claim about a date*.

**A component names one exact child version, and the reference never moves.**
`RecipeComponent.component_version` is a `PROTECT` foreign key to a specific
frozen row, and no service in the module re-points it. That is RCP-011 one level
down: a blend that changed in September must not restate what the July dish
claimed to contain. Adopting a newer child is a **new parent version**, and the
diff shows it as a change on the component's version-number row precisely so the
manager signing it can see which decision they are making.

**The child must be effective when the parent starts, and only then.** At
activation, for every applicable branch, the child must belong to the same
organization, hold an eligible frozen status, have an effective scope at that
branch, and be effective on the parent's `effective_from`. From that moment the
exact child-version reference is frozen permanently.

**The child's range is not required to cover the parent's future.** A first
implementation of Task 3.2B required that, which made an open-ended parent
demand an open-ended child and so blocked ordinary supersession forever. It was
withdrawn, and the reasoning behind it withdrawn with it.

The mistake was conflating two questions this ADR has otherwise been careful to
separate. *Selecting* a version for a new transaction is a date question, and
`resolve_recipe_version` answers it. The *validity of an already-frozen exact
reference* is not a date question at all — `component_version` is an immutable
foreign key to a specific frozen row, and superseding that row ends its
availability for new selection without reaching backwards into a parent that
already named it.

So costing and production expansion must follow `component_version` **directly**
and must never re-resolve the currently effective child by date. Re-resolving
would be the silent re-pointing RCP-072 forbids, arriving through the back door.

Nothing cascades. A dependent parent is never re-pointed and never
auto-superseded: correcting a child is versioning, exactly as correcting a
parent is (RCP-081). An `ACTIVE` parent naming a superseded child is a
**non-blocking advisory**, never a defect.

**The graph lock buys a consistent read, not sound coverage.** *(Corrected
2026-08-17. The paragraph this replaces claimed the lock closed the coverage
race; the owner-policy correction disproved that in `apps/kitchen/graph.py` and
in the concurrency tests and did not reach this file. The claim was wrong here
for a day.)*

The textbook `A → B` / `B → A` race cannot corrupt this graph, because an edge
may only be written on a `DRAFT` parent and a draft is never anybody's child — so
two concurrent additions cannot lie on one path.

What the lock genuinely buys is a **consistent read of the whole edge set**.
Cycle and depth are properties of that set and every check re-reads it, so a walk
that observed the graph half-way through somebody else's multi-edge edit could
certify a version against a picture that never existed. Certification therefore
takes the lock as well as mutation, and taking it above every row lock is what
stops opposite-order callers deadlocking.

It does **not** make coverage sound. An activation racing a child supersession
leaves a parent active from 1 July beside a child closed on 30 June — and an
ordinary sequential order produces exactly that too. Coverage is a point-in-time
gate at activation; the frozen reference is what carries the parent afterwards.

### Also settled by this extension

- `line_order` rather than `sequence`, and `FACTOR_PLACES` rather than six
  decimal places — the repository's own conventions over the specification's
  sketch, both recorded in spec §26.1.
- Cycles are judged at **recipe** identity, so `A v2 → A v1` is refused.
- Depth counts component **edges**; `MAX_COMPONENT_DEPTH = 3` (KD-08).
- Component endpoints exist, departing from §5B.2's "no component endpoint" at
  the owner's instruction, and are draft-only for mutation. Recorded in §26.5.


## Extended by Task 3.3 — the cost basis, made real

Task 3.3 implemented the half of this ADR's original scope that Task 3.2A
deferred. Nothing above is revised. Four decisions are added, and every one of
them is about the same thing this ADR has been about throughout — *what a
number is allowed to claim about a date*.

**A cost is a function of four inputs and defaults none of them.** An exact
`RecipeVersion`, a warehouse, an as-of date, and `POSTED_AS_OF`. The version
because RCP-011 says so; the warehouse because a moving average is a fact about
one store; the date because both halves of a historical question are date-driven
(RCP-026); the mode because only one of Inventory's two is reproducible.

**`POSTED_AS_OF` is the only authoritative basis, and the reason is a name
rather than a preference.** Its movement set is a *prefix* of the posting order,
so it can be identified by one integer — the organization's posted-sequence
high-water mark — captured once per calculation and stored on every snapshot.
`EFFECTIVE_DATE`'s set is not a prefix; a cost taken that way could not be
re-derived from a sequence and would disagree with itself the next time somebody
keyed in a late delivery.

That cutoff, not a lock, is what makes a card internally consistent. Costing
takes **no** inventory row locks, deliberately: locking stock so a read-only
query looks safe would let a reporting screen block a delivery, which is a worse
failure than any it prevents. A receipt racing a cost card takes a sequence above
the mark and is wholly excluded, or commits before it and is wholly included.

**Missing valuation is a refusal, not a zero.** There is no fallback price of any
kind. The card renders with the gap named — item, component path, source version
— so somebody can see what to fix, and no snapshot may be built over it. A
costing record with a hole in it is worse than no record, because it looks like a
total.

**A snapshot is append-only in the database, across all three tables.** A header
nobody may edit beside lines anybody may edit would be a document whose total no
longer agreed with the figures behind it, which is the one failure the rule
exists to prevent. Idempotency is a key **and** a fingerprint of the request; the
fingerprint deliberately excludes the resulting figures, because two identical
requests a week apart legitimately produce different totals and hashing the
answer would turn every honest re-run into a permanent conflict.

### Also settled by this extension

- **A preview exists, and does not weaken RCP-015.** A `DRAFT` or `SUBMITTED`
  version may be costed non-authoritatively, because the accountant's signature
  on `KM-RCP-004` is a signature on the *costing evidence* and asking for it
  while refusing to show the figures would be asking for a signature on nothing.
  It cannot become a snapshot and is never a historical answer.
- **Two serving answers, not one.** RCP-086's rate and RCP-087's allocation ask
  different questions; the card carries both, and the allocation is the figure
  that has to reconcile. Output left over after whole servings carries cost and
  takes its own weight.
- **The primary serving is the plate basis.** *(Corrected 2026-08-17; the first
  pass deferred plate cost to Task 3.4 and that deferral was not approved.)* No
  model carries a `portions_per_batch` column — Task 3.0 §3 sketched one and Task
  3.1 did not build it — and adding one now would be a second, **mutable**
  statement of a fact the version already holds exactly. RCP-084 guarantees one
  primary serving per version with a partial unique index, so the divisor is
  unambiguous and frozen with the version. `plate_cost` is computed as
  `total × factor_of_batch` rather than `total ÷ portions_per_batch`:
  algebraically the same, and deliberately the form that makes plate cost equal
  the primary serving's own rate *exactly* and reproduce from stored columns.
- **A serving allocation is compact, not capped.** *(Corrected the same day; the
  first pass returned only a rate above a constant.)* Equal-weight servings admit
  exactly two amounts under the certified largest-remainder rule, so two amounts,
  two counts and the leftover **are** the distribution rather than a summary of
  it. The arithmetic and the storage are therefore constant, and a
  fifty-thousand-portion scenario allocates exactly in one row. A screen may
  still limit how many example rows it lists; that decides no calculation.
- **One new Inventory module, read-only.** `apps/inventory/valuation.py`. No
  Inventory model, migration, valuation policy or posting behaviour changed, and
  Inventory still imports nothing from Kitchen.
