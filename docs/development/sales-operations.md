# Sales operations — the daily procedure, in order

**Status:** current as of checkpoint 7, 2026-08-19.
**Audience:** whoever runs a branch's day, and whoever reviews it afterwards.

Twelve screens, one procedure. Every step below names the permission it needs
and the state it leaves behind, because the commonest question this module gets
asked is "why can I not press this button", and the answer is almost always
either the state or the permission.

---

## The day, start to finish

### 1. Open the day — المبيعات اليومية → «يوم مبيعات جديد»

One document per branch per business date. The date is **entered**, never
derived from a timestamp: which day a sale belongs to depends on the branch's
own business-day start, and `date(now())` would file a 01:30 sale under the
wrong day every night.

*Needs* `create_daily_sales` at the branch. *Leaves* the day `DRAFT`.

### 2. Enter the lines

One line per menu item per channel. Each line resolves and **stores** its price
version, recipe version, serving, agreement and every field the commission used;
nothing is re-derivable afterwards, which is what lets a three-year-old line
explain itself.

Refusals you will meet, and what each means:

| Refusal | What to do |
|---|---|
| `no_effective_price` | there is no price in force for this item, branch and date. Add one on أسعار المنيو with the right effective date |
| `no_effective_recipe_version` | the recipe has no `ACTIVE` version covering this date. The kitchen owns that |
| `no_serving_on_effective_version` | the item names a serving the effective version does not offer. **Not** a fallback to the primary one: selling a whole plate where a half was meant would double every ingredient the kitchen is measured against |
| `no_effective_agreement` | an application line with no commission agreement for that branch and application |
| `application_required` | an application channel needs the company that took the order |

*Needs* `create_daily_sales`. *Leaves* the day `DRAFT`.

### 3. Declare the tenders

What the till report says each tender took. **Entered, not derived** — the
derived figure is what the lines say and this is what the operator says, and
المطابقة اليومية compares the two. A day whose declared and derived cash
disagree is a day worth looking at before it posts.

### 4. Submit — «إرسال للترحيل»

*Needs* `submit_daily_sales`. *Leaves* the day `SUBMITTED`. Lines can no longer
be added; «إعادة إلى المسودة» takes it back, with a reason, under
`create_daily_sales`.

### 5. Post — «ترحيل»

The one step that reaches the ledger. Writes the gapless number, the balanced
journal, the application receivable entries and the audit event in one
transaction: a failure anywhere leaves the day `SUBMITTED` and both ledgers
untouched.

*Needs* `post_daily_sales` — **not** held by a cashier. A till that could commit
its own takings has no second pair of eyes on the one step that reaches the
ledger.

*Leaves* the day `POSTED` and **frozen**. From here the only corrections are a
reversal, or an adjustment against one line.

---

## The drawer

### 6. Open the shift — إقفال الكاشير → «صندوق جديد»

Names the cashier whose till it is and the opening float. One shift per branch
per business date in Release 1.

*Needs* `close_cashier_shift`. *Leaves* the shift `OPEN`.

### 7. Count

Record the counted amount per tender. `APPLICATION_RECEIVABLE` is not offered
and is refused by a check constraint: a delivery company's debt is not in a
drawer, and offering a box to count it in would invite somebody to.

Recounting before the shift closes is ordinary and upserts; two rows for one
tender would make "what was counted" a question with two answers.

### 8. Close — «إقفال»

Names the **posted** sales day the drawer is reconciled against, stamps what was
expected, and computes the variance. Refused against a draft day
(`day_not_posted`): an expected figure derived from a draft could change after
the drawer was counted, and the variance would then be a difference between a
count and a moving target.

*Needs* `close_cashier_shift`. *Leaves* the shift `CLOSED`, with its counted and
expected figures **frozen**. The way back is «إعادة فتح», which is on the
record.

### 9. Approve — «اعتماد»

Somebody other than the person who closed it agrees the count, and the variance
posts. A variance of exactly zero posts no journal and is a perfectly good
outcome.

*Needs* `approve_cashier_closing`, **and** a different person: the approver may
never be the closer, enforced in the service and by a check constraint. A branch
manager legitimately holds both permissions; what they may not do is use both on
the same shift.

*Leaves* the shift `APPROVED`. Reversal needs `reverse_daily_sales`.

---

## Corrections

### 10. Adjust — المرتجعات والإلغاءات

Three reason kinds, and **the difference between them is the whole design**:

| Kind | Quantity | Reduces theoretical consumption? |
|---|---|---|
| `إلغاء قبل التنفيذ` | > 0 | **yes** — never cooked, so the ingredients never left |
| `إرجاع بعد التنفيذ` | > 0 | **no** — it was cooked, and its ingredients left through the batch |
| `تصحيح مالي` | must be **zero** | no — a money correction is not a claim that less food was sold |

Subtracting a return would lower theoretical consumption while actual stayed
where it was, manufacturing an unexplained variance of exactly the returned
quantity — in every branch that ever takes a plate back. That variance is
indistinguishable on the report from real over-portioning or real theft, which
is the failure mode. Where returned food is physically thrown away, that is a
**Waste document in the kitchen's own ledger**.

Every adjustment **reduces**. A correction that increases what was charged is a
new sales day, not an adjustment.

*Needs* `manage_sales_adjustments` at the branch — one permission covering
drafting **and** posting, deliberately, because the separation is already
achieved by who holds it at all: not the cashier, and not the accountant. A till
that can credit back its own takings is the oldest fraud in the trade.

*Reversal* needs `reverse_daily_sales`, organization-wide.

### 11. Reverse a day

Adds a mirroring journal and the opposite receivable entries; the original stays
visible. Then record a replacement day if there is one.

*Needs* `reverse_daily_sales`. *Leaves* the day `REVERSED` — and a reversed day
can never be posted again.

---

## The delivery companies

### 12. Read the ledger — ذمم التطبيقات

Append-only, and there is no balance field anywhere in the module: the balance is
`Σ debit − Σ credit` computed every time it is asked for. A stored balance is a
number that can disagree with the entries that produced it, and the disagreement
is always discovered during a settlement argument — the worst possible moment.

Aging is reported in four buckets and **nothing is ever written off
automatically**.

*Needs* `view_application_receivables`.

### 13. Settle — تسويات التطبيقات

The three-way comparison, and the reason it is three figures rather than one:

```
expected   Σ allocations over posted receivable entries
statement  what the application's own statement says
remitted   what actually arrived in the bank or the till

statement_gap  = expected  − statement
remittance_gap = statement − remitted
```

Every dinar of each gap must be **claimed** by an adjustment carrying that gap's
leg, a closed-vocabulary reason and a signed amount. Reconciliation is refused
unless both equalities hold **exactly** — not within a tolerance, because the
tolerance is where a misconfigured commission rate lives. Posting re-checks both
under the row lock, because the figures could have moved between the two acts.

Which two of the three figures agree is the diagnosis. Declared and derived
agreeing while the remittance is short is a withholding; statement and
remittance agreeing while expected is higher is a rate dispute. One "variance"
answers neither.

`فرق غير مفسّر معتمد` exists and is not free: two check constraints make it cost
a written explanation and a named approver. An unexplained difference may reach
the ledger, but only wearing a name and a reason.

*Needs* `manage_application_settlements`, organization-wide. **Not a branch
manager**, even though a branch manager may agree the commission rate — the
person who agreed the rate must not also be the person who agrees the statement
that applies it. Not an accountant either: this one permission covers posting
*and* reversal.

---

## Reading it back

### 14. المطابقة اليومية

One row per branch per business date, with every stream kept apart: declared
against derived per tender, the counted drawer beside the cash leg, the posted
returns, the receivable movement, and the cancelled quantity.

**It stores nothing and it has no acknowledge button.** A finding says two
documents disagree, and the only honest response is to change a document through
its own service. A control that marked a difference as seen would let a real
shortage be closed by clicking while the disagreement stayed in the ledger.

*Needs* `view_sales_reports`.

### 15. لوحة المبيعات

The module's landing page. The headline renders with the page; every card below
fetches itself, so the reconciliation and cost cards delay only themselves.

`صافي الإيراد` is the ledger's own arithmetic — revenue credit less the discount
and returns debits — so the number on the screen is one a person can find in the
general ledger. The application-funded discount is shown beside it and is **not**
subtracted, because the application reimburses it.

Cost, margin and food-cost figures appear **only** with `view_sales_cost` and
are **omitted, never blanked**: a blank card tells the reader a number exists and
that they are not trusted with it, which is a different statement. Those figures
are read from frozen `RecipeCostSnapshot` evidence, and a line with no snapshot
behind it is counted as uncosted rather than valued at zero.

---

## Two commands worth knowing

```powershell
# Populate a development database so every screen has rows on it.
.\.venv\Scripts\python.exe manage.py seed_sales_demo --user <username> --confirm-demo

# Ask whether everything the module claims still agrees with the ledgers.
.\.venv\Scripts\python.exe manage.py verify_sales --organization <code>
```

`verify_sales` exits non-zero only for `ERROR`. An `ADVISORY` — a commission gap
with a delivery company — and a `COVERAGE_LIMITATION` — a drawer counted but not
yet approved — are ordinary states of a working restaurant, and a verifier that
failed on them would be red every month and therefore ignored every month.

There is no `--fix`. There never will be.
