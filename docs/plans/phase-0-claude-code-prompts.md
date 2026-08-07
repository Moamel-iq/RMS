# Khan Mandi ERP — Claude Code Prompt Pack (Phase 0: Foundations)

> **Before your first prompt, confirm all four:**
> 1. `.claude/settings.json` deny-list is in place (no `DROP`, no `--fake`, no `flush`, no force-push)
> 2. `CLAUDE.md` is committed at the repo root
> 3. The architecture doc and diagram pack are in `docs/`
> 4. The Arabic PDF spike passed — a correct Arabic invoice renders via Chromium
>
> **Then start the session in plan mode** (`Shift+Tab` until it reads *plan mode*).

---

# PROMPT 1 — Session bootstrap and Phase 0 plan

*Paste this as your first message. Plan mode ON. It must not write code.*

```
You are working on Khan Mandi Restaurant ERP — a multi-branch restaurant ERP in
Django for a restaurant group in Baghdad, Iraq. I am a software engineer and I
will review every change. Work with me, not ahead of me.

## Step 1 — Read before you think

Read these in order and tell me you have read them:
- CLAUDE.md
- docs/architecture/Khan_Mandi_Restaurant_ERP_Revised_Architecture.md
- docs/diagrams/ (all files)

Do not skim. The architecture is already decided and approved. Your job is to
implement it faithfully, not to redesign it.

## Step 2 — Ask me the open questions

Before producing any plan, ask me about anything genuinely undecided. I expect
questions on at least these, one at a time:

- IQD rounding: decimal places stored, decimal places displayed, rounding mode,
  and whether amounts round to the nearest 250 IQD anywhere
- Quantity precision for base units, and the precision of unit conversion factors
- Fiscal year start month, and period granularity (monthly vs custom)
- Chart of accounts: do we follow the Iraqi unified accounting system, or a
  custom restaurant chart? Account code format?
- Inventory valuation scope: confirm Organization + Branch + Warehouse + Item
- Whether one branch may hold multiple warehouses at go-live
- Whether cost centers are required on every journal line or optional

Wait for my answers. Do not assume defaults on any of these.

## Step 3 — Produce the Phase 0 plan

Phase 0 is "Foundations". Its purpose is to build the primitives that every
later module depends on, and nothing else.

IN SCOPE for Phase 0 — as nine ordered vertical slices:

  0.1  Project skeleton, settings, Decimal/money primitives, rounding policy
  0.2  Organization hierarchy: Organization, Branch, Warehouse, KitchenLocation,
       CashPoint, CostCenter
  0.3  Authentication, roles, permissions, and branch scoping
  0.4  Units of measure and item-level unit conversion service
  0.5  Business date: BusinessDayRule + business_date_for(timestamp, branch)
  0.6  Fiscal periods and the period-lock guard
  0.7  Document base: status lifecycle, document numbering sequences,
       idempotency keys, audit events
  0.8  Accounting kernel: Account, JournalEntry, JournalLine, post_entry(),
       reversal support — the engine only, not the full chart of accounts
  0.9  i18n wiring for Arabic and English, and an RTL smoke check

EXPLICITLY OUT OF SCOPE for Phase 0 — do not create these models, do not stub
them, do not reference them:
  - Items, stock movements, inventory balances
  - Suppliers, purchase orders, receipts, invoices, payments
  - Recipes, production, waste
  - Sales, channels, settlements
  - Employees, payroll
  - Reports beyond a trial balance smoke test
  - Any AI feature

If you believe something out of scope is required, stop and ask me. Do not
quietly add it.

## Step 4 — Plan format

For each of the nine slices, give me:
  - Purpose in one sentence
  - Models and fields, with types and precision
  - Database-level constraints (unique, check, index) — not just Python validation
  - Services and their signatures
  - Selectors and their signatures
  - The invariants that must always hold
  - The tests that will prove those invariants, named
  - The edge cases you are aware you have NOT handled
  - Estimated commits

Then stop. Do not write code. Do not create files. I will approve slice 0.1
before you touch the repository.

Present the plan as a numbered document I can read on a phone. Be concise —
no filler, no restating the architecture back to me.
```

---

# PROMPT 2 — Reusable per-slice prompt

*Use this for every slice, 0.1 through 0.9. Change only the header block.*

```
Slice: 0.4 — Units of measure and unit conversion
Spec: docs/specs/foundations-units.md

Work in this exact order. Stop at each STOP and wait for me.

1. SPEC
   Write or update the spec file for this slice. It must contain: entities,
   invariants, the conversion algorithm in plain language, the rounding rule,
   effective-dating behaviour, and the edge cases you are deliberately not
   handling yet. Include a Mermaid ERD and a Mermaid flowchart of the
   conversion decision path.
   STOP. I review the spec.

2. TESTS
   Write the failing tests first. Cover at minimum:
   - every invariant listed in the spec
   - one golden case with real Khan Mandi numbers, saved to
     docs/testing/golden-cases/
   - a Hypothesis property test: converting to base and back is lossless
     within the declared precision
   - the rejection cases: unknown unit, wrong dimension, zero factor,
     negative factor, expired conversion, overlapping effective dates
   Run them. Show me they fail for the RIGHT reason.
   STOP. I review the tests.

3. IMPLEMENT
   Write the minimum correct implementation. Include the migration.
   Enforce invariants with database constraints where possible, not only in
   Python. Run the slice tests, then the full suite.
   STOP. I review the diff and the migration.

4. VERIFY
   Run: ruff format, ruff check, mypy, makemigrations --check, pytest.
   Show me the output. Do not proceed if anything is red.

5. COMMIT
   Propose one Conventional Commit per concern, with the message text.
   Do not commit until I say so.

Rules for this slice:
- Decimal only. Never float. Never round mid-calculation.
- No business logic in models, serializers, signals, admin, or views.
- If a requirement is ambiguous, ask me. Do not guess and note it later.
- If you need something from a later phase, stop and tell me.
```

---

# PROMPT 3 — The first real slice

*Use after the Phase 0 plan is approved.*

```
Begin slice 0.1 — Project skeleton, settings, and money primitives.

Follow the five-step protocol from our slice template.

Specific requirements for this slice:

- Settings split: config/settings/{base,local,production}.py, all environment
  values loaded through pydantic-settings with typed, fail-fast validation.
  A missing or malformed variable must crash at startup, not at runtime.
- TIME_ZONE = "Asia/Baghdad", USE_TZ = True. Store UTC, display Baghdad.
- A core app containing:
    - the money type and quantity type with their declared precisions
    - the rounding policy as a single function, documented, with the mode named
    - a Decimal guard: a test that fails if any float appears in a monetary
      or quantity code path
- The base abstract model: uuid pk, created_at, created_by, updated_at,
  updated_by.
- Structured logging with structlog, JSON in production.
- A health endpoint via Django Ninja that proves the API layer is wired.

Invariants I want proven by test:
- Rounding is deterministic and matches the written policy in every direction,
  including negative amounts and exact .5 cases
- No float can enter a money calculation
- Settings fail loudly on a missing required variable
- The business currency is IQD and cannot be silently changed per branch

Write docs/adr/adr-001-decimal-and-rounding.md recording the decision and the
alternatives rejected.

Start with step 1: the spec. Then stop.
```

---

# PROMPT 4 — End of session

*Run this before you close any session. It is what makes the next session cheap.*

```
Session wrap-up. Do all of the following, then stop:

1. Update the spec files for anything we changed or learned this session.
2. Update docs/requirements/traceability.md — map each requirement touched to
   the code and tests that satisfy it.
3. Append to docs/session-log.md: what we completed, what is half-finished,
   what decisions I made, and what is blocked.
4. List every deliberate shortcut or TODO we left, with the file and line.
5. Tell me the exact next action for the next session, in one sentence.

Do not summarise the conversation. Write durable state to files.
```

---

# PROMPT 5 — When you suspect it has gone wrong

*Keep this one nearby. Use it the moment something feels off.*

```
Stop implementing. Switch to review.

Audit what you have written in this session against CLAUDE.md and the slice
spec. Specifically check:

- Is there business logic outside the service layer?
- Does any code posting to a ledger bypass post_entry() or post_movement()?
- Is there a float anywhere in a money or quantity path?
- Does any journal entry construction not assert debits equal credits?
- Is any date derived with date(timestamp) instead of business_date_for()?
- Are there constraints enforced only in Python that belong in the database?
- Did you add anything outside the declared scope of this slice?
- Are there tests that assert implementation details rather than invariants?

Report findings as a list with file:line. Propose fixes. Do not apply them yet.
```

---

## How to run the session

| Do | Don't |
|---|---|
| Plan mode for every spec and every review | Let it plan and implement in one breath |
| One slice per session where possible | Chain three slices to "save time" |
| `/clear` between unrelated slices | Let context fill with dead detail |
| Read every migration yourself | Approve migrations by vibe |
| Line-by-line review on money, rounding, conversion, period locks | Line-by-line review on CRUD and admin config |
| Run PROMPT 4 before closing | Rely on the model remembering next session |

**The one failure mode to watch for:** the code will look clean and be confidently wrong about a business rule — a sign flip on a credit, a rounding direction, a conversion applied twice. Syntax is never the problem. Read the accounting logic like a reviewer who expects to find a bug.

**Slice 0.8 is the dangerous one.** The journal kernel is the piece everything else posts through. Take two sessions on it if needed, and do not let it ship without a property test asserting that no `JournalEntry` can ever exist with unbalanced lines.
