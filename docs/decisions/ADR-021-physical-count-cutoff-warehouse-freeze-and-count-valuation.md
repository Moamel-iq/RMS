# ADR-021 — Physical count cutoff, warehouse freeze and count-adjustment valuation

- **Status:** **Accepted** (2026-08-10, Task 1.6)
- **Date:** 2026-08-10
- **Related:** ADR-008 (business date), ADR-015 (cost centres), ADR-016
  (permission and scope), ADR-017 (source identity), ADR-018 (stock ledger and
  valuation), ADR-019 (account roles, control-account continuity, lock order),
  ADR-020 (transfers, in-transit, extended lock order)
- **Detail:** `docs/tasks/phase-1-task-breakdown.md` Task 1.6

ADR-018 settled what stock is worth. This settles what happens when somebody
walks into the warehouse and counts it — which is the one inventory operation
whose *procedure* is the control, not its arithmetic. A count's figures are
worth exactly what the conditions they were gathered under are worth.

## Decision

### 1. Release 1 counts a whole warehouse, and freezes all of it

`StockCountScope.FULL_WAREHOUSE` is the only value, and the enum exists so a
second one cannot be added by accident.

A partial count is not a full count with a shorter sheet. It means freezing
*part* of a warehouse, and "part" has to be something the ledger can enforce —
a per-key freeze, checked on every posting. Until that exists, offering
`SELECTED_ITEMS` would be offering a freeze that does not hold, and the count
sheet would silently be a claim about stock that moved while it was being
counted. Cycle counting is deferred, not designed and skipped.

### 2. One cutoff, snapshotted at the moment the warehouse shuts

`cutoff_at` is the single moment the book position was photographed;
`business_date` is the operating day that moment belongs to, derived through
the branch's timezone and cutoff and stored **with the settings that derived
it** (ADR-008). Both are fixed when the warehouse freezes.

Everything the count later posts is dated by that day — not by the day somebody
got round to approving it. A count of the 1st approved on the 3rd is a fact
about the 1st, and posting it into the 3rd would put a variance in a month
whose figures were already reported.

### 3. The book snapshot is a photograph, never a recalculation

Each line stores the quantity, value, average, control account and posted
sequence as at the cutoff, immutable afterwards by trigger.

Reading the current balance at approval instead would value the variance
against a position that may have moved since. If the freeze held, it cannot
have — so the two agree and the snapshot costs nothing. If the freeze did *not*
hold, silently posting against the changed figure is the worst available
response: it buries the evidence of the first fault inside a plausible-looking
second one. `count_snapshot_mismatch` is the right answer, and it names the
line.

### 4. `Warehouse.frozen_by_count` is the only statement that a warehouse is frozen

Not a boolean beside an "active count somewhere". Two mutable truths about one
fact fail in exactly the ways that matter: a warehouse frozen with no count
able to release it, or a count that believes it holds a freeze somebody else
has cleared.

Here there is nothing to disagree with. Frozen means the column is set, and the
row it names is the only thing that may clear it. Two triggers hold both
directions, and both are **immediate**:

- a warehouse may only name a count that is `IN_PROGRESS` or `SUBMITTED`;
- a count may not leave those states while a warehouse still names it.

Deferring the first was tried and is wrong: a deferred constraint trigger
captures `NEW` as it was when the statement ran and re-evaluates its queries at
commit, so the `start_count` write that froze the warehouse would be re-judged
against the count's *final* status and fail for every count that ever posted.
Immediate checking forces the services to release the freeze **before** moving
the count out of an active state, which is the order they use.

`StockBalance.is_frozen` predates this and stays. It is a different, finer
concept — a single position — and a count never writes it. Pretending a
warehouse-wide freeze is a per-position one is what §1 forbids.

### 5. The freeze holds against concurrent postings only because of a lock

Reading `frozen_by_count` is not enough. Under READ COMMITTED a posting cannot
see an uncommitted freeze, so a count could photograph a warehouse while a
posting that will land in it is already in flight.

The stock keys alone do not close it either: a first-ever receipt of an item
the warehouse has never held takes a key the count never snapshotted, precisely
because there was nothing there to snapshot.

So every posting takes a **shared warehouse freeze lock**, and starting,
cancelling or posting a count takes it **exclusively**. Sorted by id, because a
transfer holds two and two transfers running in opposite directions would
otherwise deadlock — the same defect the stock keys are sorted to avoid, one
level up.

### 6. Blind entry, by construction

The conductor is never shown the book quantity: not in the API, not in the
rendered HTML, not in a hidden field or a data attribute.

`blind_lines` returns dictionaries that have never *held* a book quantity, and
the counting screen is its own view with its own template. That is the whole
mechanism — a value that was never fetched cannot be leaked by the next person
who adds a line to a serializer.

`view_valuation` makes no difference here, deliberately. A manager who can see
cost everywhere else still gets a blind sheet, because the control is over what
the person doing the counting knows *at the moment they count*, not over what
they are otherwise entitled to look up. A counter who can see the expected
figure tends to find it, and a count that confirms the books measures nothing.

### 7. Maker-checker, in four places

`approved_by != conducted_by`, enforced in the service, at the API, by a check
constraint, and by a test. Hiding the approve button is a courtesy; a rule that
lives only in a hidden button is not a rule.

Conducting is warehouse custody — a storekeeper does it. Approving is a
branch-level authority over the figures, which is why an accounting manager
holds it and a storekeeper does not.

### 8. Valuation: losses take the average, gains split

A **loss** leaves at the standing moving average with the kernel's
full-depletion rule at zero. It is an ordinary outbound and is valued as one.

A **gain** splits:

```
book quantity > 0 and average > 0  ->  the standing average
otherwise                          ->  an explicitly approved unit cost
```

Into standing stock at the standing average, so finding more of something does
not restate what the rest of it cost. Into an empty or never-valued position
there is no average to borrow, and defaulting to zero would book free stock —
an asset that arrived from nowhere and a variance account that never took the
credit.

`zero_cost_confirmed` separates "nobody said what it was worth" from "we
looked, and it is worth nothing". Both would otherwise be the same null. A
count whose only variance is a confirmed-zero gain posts stock movements and
**no journal at all**, because nothing of value changed and an empty entry
would be a journal that means nothing.

### 9. Accounting

```
loss   Dr Inventory Count Variance   Cr Inventory Control
gain   Dr Inventory Control          Cr Inventory Count Variance
```

Grouped by account and direction and **never netted**: a count that found
300,000 of rice and lost 280,000 of chicken is not a 20,000 event, and a single
net line would report neither. The control account is the position's own,
snapshotted at the cutoff, never re-resolved — ADR-019 §7 applied to a count.

`INVENTORY_COUNT_VARIANCE` and `INVENTORY_ADJUSTMENT` both existed already; no
new role was invented. Their accounts sit in class 7 under "فروقات وتسويات"
rather than class 6, because both are **bidirectional**: a count that finds
more rice than expected is not negative spending. One account per direction was
considered and rejected — the pair would have to be netted in every report that
asks the only interesting question, which is what the variance came to. Waste
is different and is class 6, where a cost centre is mandatory.

### 10. An active count blocks closing its period

A count that freezes a warehouse on the 30th, followed by a month close on the
1st, is a count that can neither post nor usefully be cancelled: the warehouse
stays shut until somebody reopens a closed period. Refusing the close is the
cheaper of the two, and the error names the count and the warehouse.

Accounting exposes `register_period_close_guard`, the same shape as
`register_mapping_guard` and for the same reason: accounting owns the period
lifecycle and must not learn what a stock count is, while inventory must not
reach into the period state machine.

The guard runs **inside the transaction, under the period's row lock**, and
`start_count` takes that same row lock before checking the period. Without it
both can commit: neither sees the other's uncommitted work, so the close finds
no active count and the count finds an open period.

### 11. Waste extends the operational document; the count and the adjustment do not

Waste is an operational custody act — one warehouse, one business date, one
posting, one reversal — sharing the issue's whole lifecycle, numbering,
source-identity shape, locking, scope resolution and screens. What differs is
one movement type, one journal side, and two per-line fields. That is exactly
the variation `InventoryMovementDocument`'s type discriminator was introduced
to carry, and a fourth hand-copied block would be the first to miss whatever
the other three gain next.

A **count** is not one posting: freeze, snapshot, blind entry, submission,
approval by a different person, then posting. Most of those have no analogue in
a one-post document, and three exist precisely to keep two people's authority
apart.

An **adjustment** is its own aggregate for one specific reason: a single
document carries lines that go in **different directions** — a gain, a loss,
and a revaluation that moves no quantity at all. `InventoryMovementDocument`
maps one document type to exactly one movement type, and making that per-line
would push the discriminator down into the lines. `VALUE_ONLY` settles it on
its own: there is no signed movement of goods that expresses "this stock was
always worth 40,000 less than we said".

### 12. The kernel learns direction, and a third arithmetic

`MovementType.MANUAL_ADJUSTMENT` has no fixed sign, and Task 1.2 left it out of
both sign sets with a comment saying why — but `post_stock_entry` then fell
through to the outbound branch, correct for two of the three cases and silently
wrong for the third.

`MovementInput.direction` is now **required** for a signless type and
**refused** for every other. Both halves matter: without the first a gain posts
as a loss, and without the second a caller could label a `RECEIPT` as `OUT` and
take stock off the shelf through a path that never checks availability.

`apply_value_only` is the third primitive beside `apply_inbound` and
`apply_outbound`:

```
new_value   = old_value + adjustment
new_average = new_value / unchanged_quantity
```

Two refusals live in `post_stock_entry` rather than in the pure function,
because both are policy about a position rather than arithmetic: a revaluation
against zero quantity (`value_only_needs_quantity`) and one that would drive
the value below zero (`value_only_would_go_negative`).

### 13. Expired lots may leave through the three removal routes

Ordinary issue stays blocked, for everyone, exactly as ADR-018 left it. Waste,
a count loss, and an authorized negative adjustment are exempt, along with the
transfer shortage ADR-020 already exempted.

The rule exists to stop expired food reaching a kitchen through an ISSUE. An
exemption for the documents whose whole purpose is to get it *out of the
building* costs that nothing, and refusing them would leave expired stock
permanently on the books — the outcome the rule was written to avoid.

### 14. Reason codes are organization master data, with a frozen identity

Spoilage, breakage, over-portioning and theft are one restaurant group's
vocabulary. What is closed is the set of *documents* a reason can attach to,
because that is a property of this software.

The **code** and **what it applies to** are immutable once created, by trigger.
Everything else may change. Renaming `SPOIL` from "تلف" to "تلف طبيعي"
clarifies the record; repointing it at count variances rewrites it, and no
reader of a year's waste report could tell that had happened.

Archiving never deletes, so the unique constraint keeps a retired code
**reserved forever**: reissuing `BREAK` to mean something new would put two
meanings behind one identity, which is the same defect as repurposing it,
arrived at more slowly.

### 15. Lock order, extended again

ADR-020 §11's order, with the warehouse freeze above it:

```
1. the source document row (count, waste, adjustment)  select_for_update
2. the warehouse freeze locks, sorted by id            advisory, shared for
                                                       postings, exclusive for
                                                       freeze changes
3. the warehouse row, for a freeze change              select_for_update
4. the accounting period row, for a count start        select_for_update
5. the organization's account-mapping lock             shared for postings,
                                                       exclusive for mutations
6. the child rows being resolved                       select_for_update, by pk
7. every stock key the event touches                   advisory, canonical order
8. the inventory posted-sequence counter
9. the domain document-number sequence
10. the journal-number sequence
```

Steps 2, 3 and 4 are new and sit **above** the mapping lock, which is where
nothing was already standing: no posting path locked a warehouse or a period
row before Task 1.6, so adding them at the top inverts no existing order.

## Consequences

- A count is only as good as its freeze, and the freeze is now a database fact
  with a lock behind it rather than a convention.
- Reconciliation gains three comparisons: `book + posted variance == counted`
  per line, the adjustment's signed values against its movements, and every
  frozen warehouse against the count that owns it. The third catches a
  warehouse shut by nothing — invisible until somebody tries to post.
- A period cannot be closed out from under an open count, which means month-end
  now has an operational dependency on the warehouse finishing its count. That
  is the real dependency, made visible.
- Waste inherits every future improvement to the operational document, and
  every future defect. That is the trade the discriminator was accepted for.

## Alternatives rejected

**A `Warehouse.is_frozen` boolean alongside an active count.** Two mutable
truths about one fact. The Task 1.2 kernel already had a `getattr(warehouse,
"is_frozen", False)` placeholder waiting for this decision; it was replaced,
not filled in.

**Deferring the freeze-ownership trigger.** Tried, and wrong for a reason worth
recording: a deferred constraint trigger re-judges the row it captured against
queries evaluated at commit, so every count that ever posted would fail on the
statement that froze its warehouse hours earlier.

**Recomputing the book quantity at approval.** Removes the snapshot and with it
the only evidence that the freeze was bypassed.

**Valuing a zero-book gain at zero by default.** Books free stock. An omitted
answer and a deliberate zero must not be the same null.

**A separate count-gain account and count-loss account.** Every report that
asks what the variance came to would have to net them.

**Forcing the adjustment into `InventoryMovementDocument`.** Requires a
per-line movement type, which is the type-field leak Task 1.4 avoided by
keeping the type on the document — and still cannot express `VALUE_ONLY`.
