# Phase 4 — Sales: where it stands

- **Branch**: `phase/4-sales`, cut from `origin/phase/3-kitchen` at `526397b`
- **Last updated**: 2026-08-19
- **Spec**: `docs/tasks/task-4-0-sales-domain-spec.md`
- **Decisions**: ADR-027, ADR-028

> **Certification note, repeated here because it must not be lost.** The
> `phase-3-kitchen-complete` tag did not exist when this branch was created, so
> it was cut from the latest clean pushed `origin/phase/3-kitchen`. **Sales
> certification must rebase onto the Phase 3 completion tag before the Phase 4
> exit gate.**

---

## Checkpoints landed

| | Commit | What it brought |
|---|---|---|
| 0 | `9c7f580` | Task 4.0 spec, ADR-027, ADR-028, eleven `SALES` account roles, chart accounts |
| 1 | `30f06e6` | Menu categories, items, branch availability, effective-dated prices, sales channels |
| 2 | `2883ffc` | Delivery applications, commission agreements, discount programmes |
| 3 | `2a21f56` | Daily sales capture, posting, reversal, receivable ledger, kitchen `SALES` source |

## Navigation

Six of twelve sections active, six honestly inert.

| Section | State |
|---|---|
| لوحة المبيعات | inert — checkpoint 7 |
| المبيعات اليومية | **active** |
| أصناف المنيو | **active** |
| قنوات البيع | **active** |
| تطبيقات التوصيل | **active** |
| العمولات والاتفاقيات | **active** |
| الخصومات | **active** |
| المرتجعات والإلغاءات | inert — checkpoint 4 |
| ذمم التطبيقات | inert — checkpoint 5 |
| تسويات التطبيقات | inert — checkpoint 5 |
| إقفال الكاشير | inert — checkpoint 6 |
| المطابقة اليومية | inert — checkpoint 6 |

Every active route answers 200 as a full page **and** as an htmx fragment, with
no second shell inside the fragment. Verified by a route smoke over all fifteen
sales routes, not only the six in the sidebar.

## Tests

`apps/sales` — **86 passing**. Covers the cash journal, the application
journal, the append-only receivable, the posted-day freeze, the commission
bases, the discount funding split, and the kitchen registration.

## What checkpoints 4 to 7 still owe

**Checkpoint 4 — المرتجعات والإلغاءات.** `SalesAdjustment` /
`SalesAdjustmentLine` with the three closed reason kinds, their posting and
reversal, and the screen. One hook is already waiting for it:
`apps/sales/consumption_source._cancelled_quantity` returns zero today, which is
correct because no adjustment can exist yet — checkpoint 4 replaces its body
with the query over posted `CANCELLED_BEFORE_FULFILLMENT` lines. A return must
**never** be subtracted there (ADR-028 §8).

**Checkpoint 5 — ذمم التطبيقات and تسويات التطبيقات.** The receivable *ledger*
already exists and is written by posting; what is missing is the two screens and
the settlement aggregate with its allocations, its three-way reconciliation and
its blocking unexplained variance.

**Checkpoint 6 — إقفال الكاشير and المطابقة اليومية.** `CashierShift` with
maker-checker, and the over/short journal — the **only** thing a closing may
post. The reconciliation is report-only.

**Checkpoint 7 — لوحة المبيعات, demo, verifier, API, docs.** Also outstanding
from the owner's §I and §L: the whole `/api/v1/sales/` surface, the idempotent
`seed_sales_demo`, `verify_sales`, and the documentation set in §Q beyond the
spec and the two ADRs.

## Two things to check before trusting anything here

1. **`SOURCE_DOCUMENT_TYPE` is spelled upper-case on purpose.** The accounting
   kernel case-folds a source document type before persisting it, so the
   natural spelling writes one string and looks up another — a reversal that
   cannot find its own journal. Anything new that names a source identity must
   spell it the way it is stored.
2. **The kitchen's coverage codes are now computed, not constant.** Any code
   that still compares against a hard-coded `SALES_NOT_INCLUDED_PHASE_4` is
   asking a question that has a different answer depending on whether
   `apps.sales` is installed. Use `coverage_code()` and `coverage_labels()`.
