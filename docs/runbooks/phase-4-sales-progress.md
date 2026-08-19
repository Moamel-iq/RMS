# Phase 4 — Sales: where it stands

- **Branch**: `phase/4-sales`, cut from `origin/phase/3-kitchen` at `526397b`
- **Last updated**: 2026-08-19 (checkpoint 7)
- **Spec**: `docs/tasks/task-4-0-sales-domain-spec.md`
- **Breakdown**: `docs/tasks/phase-4-task-breakdown.md`
- **Decisions**: ADR-027, ADR-028
- **Invariants**: `docs/invariants/sales-invariants.md`

> **Certification note, repeated here because it must not be lost.** The
> `phase-3-kitchen-complete` tag did not exist when this branch was created, so
> it was cut from the latest clean pushed `origin/phase/3-kitchen`. **Sales
> certification must rebase onto the Phase 3 completion tag before the Phase 4
> exit gate.**

---

## Checkpoints landed

| | Commit | What it brought |
|---|---|---|
| 0 | `9c7f580` | Task 4.0 spec, ADR-027, ADR-028, eleven `SALES` account roles, seventeen chart accounts |
| 1 | `30f06e6` | Menu categories, items, branch availability, effective-dated prices, sales channels |
| 2 | `2883ffc` | Delivery applications, commission agreements, discount programmes |
| 3 | `2a21f56` | Daily sales capture, posting, reversal, receivable ledger, kitchen `SALES` source |
| 4 | `22c2cf6` | Returns, cancellations and financial corrections |
| 5 | `e5b2ebe` | Application receivables and settlements |
| 6 | `38f2a3a` | Cashier closing and the daily reconciliation |
| 7 | *this commit* | لوحة المبيعات, `seed_sales_demo`, `verify_sales`, `/api/v1/sales/`, the documentation set |

**Phase 4 is complete.** Nothing in the spec's §I, §L or §Q is outstanding.

## Navigation

**Twelve of twelve sections active. `_sections(...)` is empty for this module
and no قريباً badge remains in Sales.** The module's own `url_name` now points
at `sales:dashboard` rather than at the menu.

| Section | State |
|---|---|
| لوحة المبيعات | **active** — checkpoint 7 |
| المبيعات اليومية | **active** |
| أصناف المنيو | **active** |
| قنوات البيع | **active** |
| تطبيقات التوصيل | **active** |
| العمولات والاتفاقيات | **active** |
| الخصومات | **active** |
| المرتجعات والإلغاءات | **active** |
| ذمم التطبيقات | **active** |
| تسويات التطبيقات | **active** |
| إقفال الكاشير | **active** |
| المطابقة اليومية | **active** |

Every route answers 200 as a full page **and** as an htmx fragment, with no
second shell inside the fragment. Verified by a route smoke over all thirty-eight
sales routes — the twelve in the sidebar, the create and detail screens behind
them, and the eight dashboard card endpoints — not only the twelve.

## Tests

`apps/sales` — **270 passing**, against a database built from the migrations.
Checkpoint 7 added seventy: the dashboard aggregates and the cost omission, the
API's exact decimals and its 404-before-403 rule, the verifier's findings and
the divergences the database refuses to produce at all, and the demo seed's
guards and its idempotency.

Two tests **outside** `apps/sales` were rewritten because Phase 4 changed the
facts underneath them, rather than deleted or weakened:

- `apps/core/tests/test_shell.py::test_unbuilt_sections_are_inert` named Sales
  as its example of a module with inert entries. It now reads the module out of
  the navigation data, so it asserts the *rule* rather than one phase's
  progress, and a companion test asserts Sales has no inert entry left.
- `tests/test_phase_0_exit.py` asserted the organization's chart holds 77
  accounts. Checkpoint 0 added seventeen Sales accounts to
  `seed_chart_of_accounts` and left the count behind, so that gate had been red
  since 2026-08-19 02:38 and nobody had run it. It now asserts 94 and says why.

## The API

Registered at `/api/v1/sales/` beside the other four routers. Reads for every
master and every document, and commands named for the transition rather than
for CRUD — there is no `PATCH` and no `DELETE` on anything that has left `DRAFT`
or `OPEN`, because a verb that implied otherwise would be the API contradicting
a database trigger.

Six domain codes were added to `CONFLICT_CODES` in `config/api.py` so they
answer 409 rather than 422: `already_posted`, `adjustment_reversed`,
`approver_is_the_closer`, `day_reversed`, `settlement_reversed`,
`shift_reversed`. `unexplained_variance`, `day_not_posted` and
`cost_center_required` stay 422 — each is something the caller can fix by
sending or recording something different.

`GET /dashboard` carries **no** cost key for anybody, and `GET /dashboard/cost`
answers 403 without `view_sales_cost`. That split is deliberate: a Ninja
response schema fills an unset optional field with `null`, and a null food cost
says a number exists and that the caller is not trusted with it. Absence had to
be structural.

## The demo

`seed_sales_demo`, namespace `DEMO-SALES-V1`, `DEBUG` only, `--confirm-demo`
before anything posts. Five menu items, four channels, three fictional delivery
applications with three different commission bases, three discount programmes
covering all three funding shapes, a posted day, a reversed day, a draft day,
one adjustment of each reason kind, one posted settlement with a claimed gap on
each leg, one reconciled settlement carrying an `UNEXPLAINED_APPROVED` claim,
and one approved drawer with a small shortage.

**Proved idempotent on the development database**: `41 created, 0 reused` on the
first run, `0 created, 41 reused` on the second, with every one of twenty tables
byte-identical afterwards — journals and receivable entries included, because a
second run that re-posted a day would leave the document count unchanged and the
ledger doubled.

Business dates are **fixed**, not relative. `SalesDay` is unique per branch and
business date, so a relative anchor would make tomorrow's run create a second
set of days rather than find the first. The command prints the dashboard URL
with the scenario's dates already in it, which is where that cost is paid.

There is no `--reset-demo`, deliberately: everything the command posts is ledger
history and none of it may be removed to make a reseed convenient.

## The verifier

`manage.py verify_sales` — fifteen sections, read-only, no `--fix`. It composes
`apps/sales/reconciliation.py`, `apps/sales/daily_reconciliation.py` and the
kitchen's coverage registry rather than re-deriving any of them.

On the development database's demo dataset: **0 ERROR, 2 ADVISORY, 0
COVERAGE_LIMITATION, exit 0.** The two advisories are a commission gap with a
fictional delivery company and a till that came up short — both real, both for a
person to decide about, and neither a defect in this software. A verifier that
exited non-zero on either would be red every month and therefore ignored every
month.

Writing it found two defects **in itself** that a clean-data-only test would
never have caught: it compared gross revenue against journals filtered to
`POSTED` events, so a reversed day's original credit was counted while its
mirror was not; and it compared each application's subledger against the whole
control account, reporting three failures on a perfectly correct set of books.
Both are fixed and both now have tests.

## What checkpoint 7 discovered about the guards

Writing the verifier's negative tests turned up something worth recording: **the
obvious way to plant a defect does not work.** `queryset.update()` past the
services is refused on every posted row in this module —
`sales_day_line_follows_its_day`, `sales_adjustment_is_frozen`,
`sales_shift_is_frozen`, `sales_receivable_is_append_only` and
`accounting_posted_line_is_immutable` between them cover the lot.

So the tests assert the **refusal** by name, and the divergences they do
manufacture are the ones a production database really produces: an account
mapping repointed after the journals that used it posted, an append to an
append-only ledger, and a journal posted through the kernel at a source identity
that names no document.

## Two things to check before trusting anything here

1. **`SOURCE_DOCUMENT_TYPE` is spelled upper-case on purpose.** The accounting
   kernel case-folds a source document type before persisting it, so the natural
   spelling writes one string and looks up another — a reversal that cannot find
   its own journal. Anything new that names a source identity must spell it the
   way it is stored. `verify_sales` check 12 asserts it.
2. **The kitchen's coverage codes are computed, not constant.** Any code that
   still compares against a hard-coded `SALES_NOT_INCLUDED_PHASE_4` is asking a
   question that has a different answer depending on whether `apps.sales` is
   installed. Use `coverage_code()` and `coverage_labels()`.

## Known gap, recorded honestly

The **development database** carries one cashier shift, `CS-2026-00001`, whose
counted figure is unrealistic. It was written by the first run of
`seed_sales_demo` before a defect in the seed was fixed: the expectation was
read from a shift that had not yet named its day, so the count was taken against
the opening float alone. The seed is corrected and a fresh database produces the
intended small shortage; the existing row was **not** edited or deleted, because
a posted journal and an approved cash difference are ledger history and this
repository does not rewrite those to tidy a demo. `verify_sales` reports it as
the advisory it is.
