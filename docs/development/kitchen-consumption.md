# Kitchen consumption, the movement partition, and usage variance

What Task 3.8 built, how to run it, and — mostly — which numbers it refuses to
produce and why.

Read `docs/decisions/ADR-026-kitchen-custody-consumption-and-usage-variance.md`
first for the reasoning. This page is the operator's and maintainer's view.

---

## The one idea

Consumption is answered by a **partition**, not a formula.

`apps/kitchen/consumption.classify_kitchen_movement` places every posted
`StockMovement` at a kitchen warehouse into exactly one of fifteen buckets. It
**raises** on a `MovementType` it does not know. Everything else — the actual
consumption report, the variance diagnostic, the API — is a *reading* of that
partition rather than a second calculation.

The partition carries its own proof, per `(warehouse, item, lot)` over the
window:

```
closing quantity − opening quantity  =  Σ (every bucket's signed total)
```

The left side is the ledger's own `quantity_before` / `quantity_after`. The right
side is built by adding up the buckets. They can only agree if the
classification is exhaustive, which is what makes it a checkable claim rather
than a table of good intentions.

**The identity is shown on screen**, in the `فرق البرهان` column of تدفق مخزن
المطبخ. A non-zero value there means a movement reached the ledger and no bucket.

---

## The fifteen public buckets

| Bucket | Movement type | Consumption? |
|---|---|---|
| `OPENING` | `OPENING` | No — the starting point |
| `SUPPLY_RECEIPT` | `RECEIPT` | No |
| `CUSTODY_TRANSFER_IN` | `TRANSFER_IN` | **No — custody, not usage** |
| `CUSTODY_TRANSFER_OUT` | `TRANSFER_OUT` | **No — including material sent back** |
| `PRODUCTION_CONSUMPTION` | `PRODUCTION_OUT` | **Yes** |
| `PRODUCTION_OUTPUT` | `PRODUCTION_IN` | No — the batch's own product |
| `DIRECT_ECONOMIC_ISSUE` | `ISSUE` | **Yes** |
| `ECONOMIC_RETURN_OR_REVERSAL` | `RETURN_IN` | Reduces direct consumption |
| `RAW_MATERIAL_WASTE` | `WASTE`, ingredient | Reported beside consumption |
| `PRODUCED_OUTPUT_WASTE` | `WASTE`, recipe output | Beside, never expanded to ingredients |
| `COUNT_GAIN` / `COUNT_LOSS` | `COUNT_GAIN` / `COUNT_LOSS` | No — corrections stay corrections |
| `VALUE_ONLY_ADJUSTMENT` | `MANUAL_ADJUSTMENT`, zero quantity | No — nothing left |
| `OTHER_QUANTITY_CORRECTION` | `MANUAL_ADJUSTMENT`, non-zero | No |
| `REVERSAL` | `REVERSAL` | Nets against whatever it cancelled |

Fifteen public buckets — the approved vocabulary, and no more.

## The three internal subcategories

Two movement types need **drill-down detail** rather than a public bucket of
their own, and one bucket genuinely holds two kinds of event:

| Public bucket | Subcategory | Movement type | Nets against |
|---|---|---|---|
| `ECONOMIC_RETURN_OR_REVERSAL` | `ISSUE_RETURN_IN` | `RETURN_IN` | direct economic consumption |
| `ECONOMIC_RETURN_OR_REVERSAL` | `SUPPLIER_RETURN_OUT` | `RETURN_OUT` | **supply** |
| `CUSTODY_TRANSFER_OUT` | `TRANSIT_SHORTAGE_LOSS` | `TRANSFER_SHORTAGE` | custody out |

This is what keeps the arithmetic safe. A supplier return reverses a *receipt*,
not a use — so netting the whole return bucket against consumption would make
goods sent back to a supplier look like the kitchen having cooked less.
`direct_economic_consumption` nets only the `ISSUE_RETURN_IN` share;
`supply_receipt` nets the `SUPPLIER_RETURN_OUT` share.

Subcategories are internal. They are never a reporting dimension of their own,
and `MovementBucket` stays at fifteen. ADR-026 §2.1 records why an earlier
seventeen-bucket version was wrong.

Waste splits by asking whether the item is any recipe's `output_item` in that
organization — a data question with a closed answer, never the document's
translated display text.

---

## Running it

```
python manage.py verify_kitchen --user <username>
```

`--user` is not optional in practice. The consumption reads are scoped by
**warehouse membership**, so without a caller they read nothing and the partition
check would report clean by examining zero movements. The command says so
explicitly and reports `kitchen_partition_not_checked` rather than passing.

Three severities. Only `ERROR` affects the exit code:

- `ERROR` — a real disagreement. Exit 1.
- `ADVISORY` — worth attention. Exit unchanged.
- `COVERAGE_LIMITATION` — something knowably absent. Exit unchanged.

`SALES_NOT_INCLUDED_PHASE_4`, `MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED` and
`FINAL_SALES_USAGE_VARIANCE_NOT_AVAILABLE` are the third class. A verifier that
went red because Phase 4 has not happened would be permanently red and therefore
permanently ignored.

There is no `--fix`.

---

## The screens

| Screen | Route name | Shows |
|---|---|---|
| تدفق مخزن المطبخ | `kitchen:report_warehouse_flow` | The partition, with the identity column |
| الاستهلاك الفعلي | `kitchen:report_actual_consumption` | Ten streams kept separate |
| متطلبات الإنتاج القياسية | `kitchen:report_production_standard` | Plan against actual, per posted batch |
| الاستهلاك النظري | `kitchen:report_theoretical_consumption` | Meal equivalents + the named sales gap |
| انحراف الاستهلاك | `kitchen:report_usage_variance` | Two outputs, labelled apart |
| استهلاك دفعة الإنتاج | `kitchen:report_batch_consumption` | One batch, with its evidence checks |
| مكافئ الوجبات | `kitchen:report_meal_equivalent_staff` / `..._complimentary` | Explained non-sales usage |

All five list reports use Task 3.6's `KitchenReportView` — scope, filters,
pagination, HTMX, CSV and the structural cost redaction come from there. The two
single-document screens have their own templates because their subject is one
document rather than a filtered list.

---

## What it will not tell you, and why

**There is no final usage variance.** `actual consumption − theoretical sales
consumption` needs approved sold quantities, which arrive in Phase 4. It is not
approximated, because a number that silently omits sales is not a rough version
of the real number — it is a different number with the same name, and it will be
wrong in the direction that looks like theft.

What you get instead is a **partial diagnostic**, stamped `PARTIAL_COVERAGE` and
`NOT_FINAL_USAGE_VARIANCE` on every row and in every export, whose residual is
named `unexplained_by_production_plan` rather than "variance".

**Meal equivalents are shown beside that residual, never subtracted from it.** A
staff meal does not consume store stock; its ingredients already left through the
batch that cooked them. Subtracting them would remove a quantity counted once and
drive the residual negative for any kitchen that feeds its staff.

**There is no combined theoretical total.** A batch plan and a meal expansion
overlap physically, and no key links a portion to the batch that produced it.

**A link changes nothing.** `BatchDocumentLink` makes a waste document or a
custody transfer findable from a batch. It does not reduce what the batch
consumed, and `batch_actual_consumption` does not read the table.

---

## The variance disclosure that is easy to miss

`comparable_consumption` sums only actual rows whose unit shares the plan item's
**dimension**, and it is right to: kilograms and litres do not add.

But a requirement can be met with *some* rows in the plan's dimension and *some*
outside it. The demo carries exactly that: 15 KG of rice planned, met with
11.25 KG rice + 2 KG cooked rice + **1.5 L of oil**. The variance is −1.75 KG,
which is honest arithmetic over the kilogram rows and leaves the litres out
entirely.

"Used 1.75 KG less than planned" and "used 1.75 KG less than planned, and also
put in 1.5 L of oil" are materially different statements. So the row carries
`PARTIALLY_COMPARABLE_DIMENSIONS_EXCLUDED`, an `excluded_rows` list, and an
Arabic sentence naming what is outside the number — on screen, in the CSV and in
the API.

`production_standard_variance` keeps such a row **even when its variance is
zero**, because the zero is only zero over the rows it could account for.

---

## Where things live

```
apps/kitchen/consumption.py                 partition, batch + period reads, standard variance
apps/kitchen/consumption_sources.py         theoretical source interface, meal adapters, coverage
apps/kitchen/consumption_reconciliation.py  usage variance analysis, the Task 3.8 verifier checks
apps/kitchen/document_links.py              create / cancel attribution
apps/kitchen/consumption_views.py           the seven screens
apps/kitchen/migrations/0022, 0023          BatchDocumentLink, its guards and the attribution cap
apps/kitchen/management/commands/verify_kitchen.py
```

## Related

- `docs/decisions/ADR-026-kitchen-custody-consumption-and-usage-variance.md`
- `docs/decisions/ADR-025-production-posting-value-conservation-and-reversal.md`
- `docs/development/production-posting.md`
- `docs/invariants/kitchen-invariants.md` — invariants 101 – 118
- `docs/runbooks/phase-3-deferred-verification.md` — rows 18 – 32
