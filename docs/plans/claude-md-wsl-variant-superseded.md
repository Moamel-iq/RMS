# Khan Mandi Restaurant ERP

Multi-branch restaurant ERP. Django modular monolith. Baghdad, Iraq. Bilingual
Arabic/English with RTL. Currency IQD.

The architecture is decided and approved. Implement it faithfully. If you think
a decision is wrong, say so and stop — do not route around it.

## Stack

- Django 5.2 LTS, Python 3.13, PostgreSQL 18 (UTF-8, ICU collation)
- Django Ninja + Pydantic for the API. Not DRF.
- uv for dependencies. Never pip install directly.
- pytest-django, factory_boy, Hypothesis
- Celery + Redis. `task_acks_late = True`, `task_reject_on_worker_lost = True`
- ruff (format + lint), mypy + django-stubs
- Timezone `Asia/Baghdad`, `USE_TZ = True`

## Commands

```bash
uv run pytest                                    # tests
uv run pytest apps/inventory -x                  # one app, stop on first fail
uv run ruff format . && uv run ruff check --fix .
uv run mypy apps config
uv run python manage.py makemigrations --check --dry-run
make reset-db && make seed
```

## Non-negotiable invariants

Violating any of these is a defect, not a style disagreement.

1. **Two append-only ledgers.** `StockMovement` for goods, `JournalEntry` /
   `JournalLine` for money. Nothing else mutates stock or financial state.
2. **Posted records are immutable.** No edit, no delete. Corrections happen by
   reversal: append a mirrored entry, then post the correct one. Original,
   reversal, and replacement all remain visible.
3. **Every journal entry balances.** Total debits equal total credits, asserted
   in the engine and enforced in the database. No exceptions, no rounding slack.
4. **Decimal only.** Money, quantities, rates, costs, yields, percentages.
   A float in a financial path is a bug. Never round mid-calculation — round
   once, at the boundary, per the written policy in
   `docs/adr/adr-001-decimal-and-rounding.md`.
5. **Moving weighted average costing**, scoped to Organization + Branch +
   Warehouse + Item. Issues do not change the average. Receipts do.
6. **Negative stock is prohibited.** Zero-cost receipts are prohibited.
7. **Business date is not `date(timestamp)`.** Service runs past midnight.
   Always call `business_date_for(timestamp, branch)`. Never derive it inline.
8. **Closed fiscal periods reject postings.** Reopening is an authorized,
   audited action.
9. **Effective dating is real.** Recipe versions, supplier agreements, channel
   commission terms, unit conversions. A July transaction resolves the version
   effective in July, never "the current one".
10. **Idempotency keys on every posting command.** A retried request must not
    double-post.

## Layering — what may call what

```
API / UI / Admin / Commands
        ↓
Service layer (commands)     Selector layer (queries)
        ↓                             ↓
Domain rules + Posting policy         ↓
        ↓                             ↓
Stock engine + Journal engine         ↓
        ↓                             ↓
        Models + DB constraints ←─────┘
```

**Business logic lives in the service layer.** Never in:
- serializers or Pydantic schemas
- views or routers
- Django signals (notifications only)
- `Model.save()` overrides
- Django admin
- raw SQL in reports

Reads go through selectors. Writes go through services. A service that posts to
a ledger must do so inside a single database transaction covering the document
status change, the ledger writes, and the audit event — all or nothing.

## Working protocol

For every slice, in this order, stopping for review at each step:

1. **Spec** — write or update `docs/specs/*.md` first. Include invariants and
   the edge cases you are not handling.
2. **Tests** — write failing tests before implementation. Prove they fail for
   the right reason.
3. **Implement** — minimum correct behaviour. Migration included.
4. **Verify** — ruff, mypy, migration check, full suite. All green.
5. **Commit** — one concern per commit, Conventional Commits, wait for approval.

Prefer database constraints over Python-only validation. If an invariant can be
expressed as a `CheckConstraint` or `UniqueConstraint`, express it there too.

## When to stop and ask

Stop and ask rather than guessing:
- A rounding direction or precision is not written down
- A posting rule's debit/credit direction is ambiguous
- A requirement needs a model from a later phase
- An edge case has real money consequences and no documented answer
- You are about to add something outside the declared scope of the slice

"I assumed X and will note it" is not acceptable on financial logic.

## Never do

- `migrate --fake`, `migrate zero`, `flush`, drop or recreate the database
- `git push --force`, `git reset --hard`
- Edit an existing migration that has been committed
- Delete or edit posted ledger rows
- Read `.env` or any secret file
- Introduce a dependency without asking
- Write code during plan mode

## Documentation map

- `docs/architecture/` — the approved architecture
- `docs/diagrams/` — Mermaid, diagrams as code
- `docs/specs/` — one spec per module, with ERD and state machine
- `docs/adr/` — decisions and rejected alternatives
- `docs/accounting/posting-rules/` — one file per business event, with the
  debit/credit mapping and a sequence diagram
- `docs/testing/golden-cases/` — worked examples with real numbers
- `docs/requirements/traceability.md` — requirement to code to test
- `docs/session-log.md` — durable session state

## Build order

Phase 0 Foundations → 1 Inventory → 2 Procurement & AP → 3 Recipes & Production
→ 4 Sales & Settlements → 5 Accounting & Treasury → 6 HR & Payroll →
7 Reports & Close → 8 Controlled AI.

Do not start a phase before the previous one meets its Definition of Done.

## Language

Code, comments, commits, and docs in English. User-facing strings translatable,
Arabic and English. Model fields that users see carry `name_ar` and `name_en`.
