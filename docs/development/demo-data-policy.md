# Demo data policy

**Status:** adopted · **Applies from:** Task 1.6a (inventory demo pass) ·
**Scope:** every task that ships a user-visible section or workflow

This is a development convention, not an architecture decision. It constrains
how we *populate* the application for inspection; it changes nothing about how
the application behaves. No ADR: nothing here is a durable design commitment
that a future phase would have to argue against — if the convention stops
earning its keep, delete the commands and this file.

## Why it exists

A screen that has never rendered a row has not been reviewed. Tests prove that
a posting service computes the right numbers; they do not prove that the Arabic
column headers line up, that a status badge reads sensibly, that the RTL table
does not overflow, or that a list is reachable at all from the navigation rail.
Those are answered by looking, and looking needs data.

Before this policy, looking required hand-building an organization, a chart of
accounts, ten account mappings, five items with conversions, and a dozen posted
documents through the UI — perhaps an hour per review, repeated by every person
who wanted to see the same screen. That cost is why screens went unreviewed.

## The rule

> Every task that creates a user-visible section or workflow must also create
> or extend safe demo-data tooling, so the feature can be exercised visibly.

A task is not done when its tests pass. It is done when someone can run one
command and *see* the thing working.

## What demo data must be

**Identifiable.** Every record the tooling creates carries a `DEMO` code or
reference. Anyone looking at a database can tell in one glance what is real and
what is scaffolding. Codes are prefixed (`DEMO-RICE`); documents carry the
namespace in their evidence reference (`DEMO-INVENTORY-V1/OPENING-01`).

**Namespaced by version.** The namespace ends in a version — `DEMO-INVENTORY-V1`.
When a later task needs a materially different scenario, it takes `V2` rather
than mutating what `V1` posted. Posted ledger history is append-only; a demo
scenario that has posted is history too.

**Idempotent.** Running the command twice produces the state of running it once.
The second run reports `reused`, not `created`, and adds no second document, no
second movement, and no second journal. This is not optional politeness: a
command that duplicates on retry cannot be run by someone who is unsure whether
it already ran, which is everyone.

**Development-only.** The command refuses to run when `settings.DEBUG` is false,
before it reads anything else. Posted operational data additionally requires an
explicit `--confirm-demo`. There is no environment variable, no settings flag,
and no `--force` that turns either guard off.

**Never automatic.** Demo data is never inserted by a schema or data migration,
never by `AppConfig.ready`, never by a signal, and never by a test fixture that
also runs in development. Someone types the command, or it does not happen.

**Explicit about ambiguity.** The command never picks among several
organizations, branches, or users on the caller's behalf. An ambiguous or
missing argument fails with a message that lists the valid choices.

## What demo data must not be

**Not ORM-written business events.** Master data may go through the approved
master-data services. Every *posted* operation goes through the real domain
service the API and the UI call — `post_opening_document`, `post_document`,
`dispatch_transfer`, `post_receipt`, `post_shortage`, `approve_count`,
`post_adjustment`, `reverse_*`. Never `StockLedgerEntry.objects.create`, never
`JournalEntry.objects.create`, never a hand-written `StockBalance`.

The reason is that the demo dataset's only value is being *real*. A stock
balance written directly is a balance no posting rule produced, reconciling
against a journal no valuation kernel computed. It would show the screens
working while proving nothing, and it would break reconciliation — which is
itself one of the screens under review.

**Not a way around an invariant.** Authorization, audit, accounting, and
inventory invariants hold for seeded data exactly as for typed data. If the
scenario cannot be built without weakening a constraint, the scenario is wrong,
not the constraint.

**Not real people or counterparties.** No real supplier, employee, payroll, or
investor information, ever, including in a name field that "will not be
committed".

**Not an approval that looks real.** Where a module records *evidence* behind an
approval, the demo dataset must name its evidence as fictional and the database
must refuse the alternative. `apps.kitchen` is the first module with this shape:
`ApprovalEvidenceKind` is `SIGNED_FORM` or `DEMO_FICTIONAL`, and a trigger
permits `DEMO_FICTIONAL` only inside the `DEMO-` namespace **and refuses
`SIGNED_FORM` inside it**.

The second direction is the one that matters and the one a policy written from
first principles would miss. A demo recipe carrying what looks like a signed
`KM-RCP-004` reference is exactly how unapproved figures acquire authority
(RCP-126): somebody screenshots the costing screen, the signature is there, and
by the time anybody checks, the number has been quoted in three meetings.

**A demo dataset that exercises an approval workflow needs real separate
actors.** `seed_kitchen_demo` creates four namespaced data actors —
`demo-kitchen-reviewer`, `demo-store-reviewer`, `demo-cost-reviewer`,
`demo-recipe-approver` — each with an unusable password, exactly as
`seed_inventory_demo`'s count conductor has since Task 1.6. Reusing one user
would produce an approval the real system refuses, which is the opposite of what
a demo is for.

**Not a licence to delete.** `--reset-demo` may remove only records carrying the
command's own namespace, and only where removal is legitimate. It never runs a
general flush, never resets migrations, never touches a record it did not
create, and never deletes or mutates posted stock or accounting history to make
a reseed convenient. Where posted history blocks a clean reset — which is the
expected and correct outcome — the command says so honestly and offers a fresh
namespace version instead.

## What a demo command must report

Exactly what it did: which organization, which branches, which user should sign
in, what was created, what was reused, and the URLs worth opening. Output goes
through `apps.core.console.SeedCommand.write`, because Arabic on a Windows
cp1252 console raises `UnicodeEncodeError` inside an atomic command and rolls
back everything already seeded — a crash caused entirely by the logging.

## What a demo command must be tested for

At minimum: refusal outside `DEBUG`; refusal without `--confirm-demo`; safe
failure on an ambiguous selector; a second run creating no duplicate documents,
movements, or journals; only namespaced master data created; posted operations
reaching the ledger through services; and reconciliation clean afterwards.

## `seed_inventory_demo` in detail

### Running it

```powershell
.\.venv\Scripts\python.exe manage.py seed_inventory_demo --user <username> --confirm-demo
```

| Option | Default | Meaning |
|---|---|---|
| `--user` | *required* | Username, email or id of the person who will sign in to review. Never guessed: two matches ends the command. |
| `--organization` | `DEMO-KHAN-MANDI` | Created on demand under its own code only. Any other code must already exist, and then only `DEMO`-prefixed master data is added inside it. |
| `--source-branch` | `DEMO-BUNOOK` | Where the stock starts. |
| `--destination-branch` | `DEMO-SECOND` | Where the cross-branch transfers go. |
| `--confirm-demo` | off | Required before anything **posts**. Without it, master data only. |
| `--reset-demo` | off | Removes what can legitimately be removed, first. |

### Namespace

`DEMO-INVENTORY-V1`. Every document carries `DEMO-INVENTORY-V1/<slug>` in its
evidence reference (a stock count carries it in `reference`), and every step
looks there before creating anything.

The namespace is **not** minted into `public_id`. Posted documents derive their
own source identity and idempotency key from `public_id`, which the services
generate — so the seed makes documents *findable* rather than taking over their
identity. The source identities stay real because the services still derive
them.

The version suffix is load-bearing. A later scenario that needs materially
different postings takes `V2`; it does not mutate what `V1` posted, because a
demo posting is ledger history and ledger history is append-only.

### Safety

1. `settings.DEBUG` is checked **first**, before any argument is read. There is
   no flag that turns it off.
2. `--confirm-demo` gates only the irreversible half. Master data can be
   recreated; a posted movement and its journal cannot.
3. Selectors never guess. An unknown or ambiguous `--user`, an unknown
   organization, or one branch named as both ends of a transfer ends the
   command with the valid choices listed.
4. The whole scenario runs in one transaction. A run that fails half way
   through leaves nothing, because the half that posted would be real.

### Idempotency

A second run reports `0 created, N reused` and adds no second document, no
second movement and no second journal. Balances are byte-identical afterwards.
Both are asserted in `apps/inventory/tests/test_demo_seed.py`.

### Reset is deliberately incomplete

`--reset-demo` deletes draft documents and draft transfers, and — only when
nothing has posted — the unused master data. It never deletes a posted stock
movement or journal entry; where they exist it says so and stops:

```
kept    37 posted stock movements and 23 journal entries — append-only, and
        never deleted to make a reseed convenient
```

Reason codes are **archived, not deleted**: a code stays reserved once used,
and a database trigger enforces it. A reseed revives the archived codes, since
an archived reason cannot be selected and the waste and adjustment steps would
otherwise be refused.

To start genuinely from nothing, use a fresh namespace version or a fresh
development database. Both leave the ledger's guarantee intact.

## What the scenario contains

Master data: 5 categories, 3 package units (`SACK`, `CARTON`, `CONTAINER`),
5 items, 5 conversions (one `VARIABLE`), 6 warehouses (2 system in-transit),
2 lots, 4 reason codes, 10 account-role mappings and 1 category-level
inventory-account override.

Posted operations, all through the real domain services:

| # | Step | What it demonstrates |
|---|---|---|
| 1 | Opening stock | Every item's starting balance, submitted and posted by two different people |
| 2 | Receipt — fixed package | 2 SACK becomes exactly 60.000 KG |
| 3 | Receipt — variable package | 2 CONTAINER weighed 35.650 KG; planning factor and implied actual factor both kept |
| 4 | Receipt — expired lot | Expired goods may be received; issuing them is what is refused |
| 5 | Issue | Consumption against a cost centre |
| 6 | Return | Valued at the original issue's cost, not today's average |
| 7 | Reversal | Posting and reversal both visible, as separate ledger entries |
| 8 | Transfer — completed | Dispatched and fully received |
| 9 | Transfer — partially received | 80.000 left in transit, so the in-transit screen has rows |
| 10 | Transfer — closed with shortage | dispatch = receipt + shortage, exactly |
| 11 | Waste — spoilage | Reason code and cost centre required |
| 12 | Waste — expired lot | Full depletion: zero quantity surrenders the whole remaining value |
| 13 | Count — posted | Conducted and approved by two different people |
| 14 | Count — cancelled | Freeze released, history kept |
| 15 | Count — in progress | Blind count sheet, warehouse frozen |
| 16 | Count — submitted | Awaiting a second person, warehouse still frozen |
| 17 | Adjustment — quantity loss | Books were wrong downwards |
| 18 | Adjustment — quantity gain | Explicit unit cost, never the standing average |
| 19 | Adjustment — value only | Value moves, quantity does not |
| 20 | Drafts | One unposted receipt, one undispatched transfer |

## htmx

Classification **A — actively and meaningfully used**, after this pass. Before
it the only `hx-*` attributes in the repository were four on the sign-in form,
which is classification D.

- **Version** 2.0.4, read from `static/vendor/htmx.min.js` by a test rather
  than trusted from a comment.
- **Vendored, never a CDN.** A test walks every template and fails on
  `unpkg.com`, `cdn.jsdelivr.net`, or any `//cdn.` host.
- **Included once**, in `templates/base.html`, with `defer`. Counted in the
  rendered page, because inheritance is what would cause a duplicate.
- **Used by** the sign-in form (`hx-post` → fragment, `HX-Redirect` on success)
  and every inventory list screen (`hx-get` → results partial).
- **Target** `#list-results`, **swap** `outerHTML`, **history** `hx-push-url`.
- **Fallback**: the toolbar is still a `method="get"` form with a submit
  button. Without JavaScript it submits normally and the server returns the
  whole page.

Screens still using full-page requests: every detail and form screen —
document detail, line entry, transfer dispatch and receipt, count sheet entry,
approval and posting actions, and every settings/accounting screen.

### The partial without duplicating the table

`settings/base_list.html` extends a **variable** parent:
`shell.html` normally, `settings/_list_fragment.html` when the view answers an
`HX-Request`. The fragment emits only the `results` block. There is one copy of
the table markup, and a column added to a list appears in its partial
automatically — an `{% include %}` could not see the child's blocks.

`hx-*` attributes are gated on `htmx_list`, which only views that answer the
partial set. A list whose view returns a whole page therefore never gets them,
because swapping a page into a table would nest the shell inside itself.

## Current commands

| Command | Namespace | Covers |
|---|---|---|
| `seed_inventory_demo` | `DEMO-INVENTORY-V1` | Inventory master data, opening stock, receipts, issues, returns, reversal, transfers, in-transit, shortage, waste, stock counts, manual adjustments, reorder points, dated lots, import batches |
| `seed_procurement_demo` | `DEMO-*` procurement codes | Suppliers, catalogue, requests, quotations, award, orders, revision, receipts, invoices, matching, returns, credit notes, payments, report routes, applied and rejected import batches. (This row was missing while the command shipped through Phase 2 — the table lagged the code by a phase, found at Task 3.0.) |
| `seed_kitchen_demo` (Task 3.1) | `DEMO-KITCHEN-V1` | Five recipes: one batch recipe with FOOD and PACKAGING lines, a substitute, numbered steps (one with a sourced duration, one with a qualitative heat instruction and a **null** temperature) and three servings; one portion recipe drawing on the batch's output; one draft with no structure; one recipe with no draft; one archived. Creates the single permitted new item `DEMO-RICE-COOKED`. **No approved version, no cost, no price, no stock movement and no journal entry.** Every row carries `تجريبي — غير معتمد للإنتاج` |

Reference data seeds — `seed_units`, `seed_chart_of_accounts`,
`sync_accounting_roles` — are **not** demo commands. They create deterministic
reference data that production genuinely needs, carry no `DEMO` namespace, and
have no `DEBUG` guard. Do not add demo records to them.

## Task 1.7A additions

The rule at the top of this file — every user-visible feature ships demo data —
was applied to the reports and the import history. `seed_inventory_demo` gained:

- **Reorder points** placing one item below its point, one exactly on it, and
  one above, so the reorder report shows all three states rather than a single
  colour.
- **Three dated chicken lots** — one already expired and holding stock, one due
  in twenty days, one in ninety — so the expiry report has a row in every
  bucket. The expired batch is left standing rather than written off: it is
  what the screen exists to surface, and waste is the flow that clears it.
- **One APPLIED import batch**, built through the real import service, which is
  what sets those reorder points.
- **One FAILED_VALIDATION batch** containing a perfectly good row that was
  correctly never applied. The good row is the point: a batch of pure rubbish
  proves only that validation rejects rubbish, while a mostly-right batch
  proves the harder rule.

Both batches are found by filename on re-run, so a second seed reports them as
reused and creates no third batch.


## The nested-recipe demo graph (Task 3.2B)

`seed_kitchen_demo` adds three more recipes, and they exist to make one
distinction visible that no amount of prose conveys:

    DEMO-RCP-DISH v1  →  DEMO-BLEND-MARINADE v1  →  DEMO-BLEND-SPICE v1
    DEMO-RCP-DISH v2  →  DEMO-BLEND-MARINADE v2  →  DEMO-BLEND-SPICE v1

Both dish versions also carry `DEMO-RICE-COOKED` as an ordinary **line**. That
is the point of the scenario: the semi-finished item has a book value and is
consumed at it, while the marinade beside it has no book value and is expanded
from its exact child version. Adding the stocked item as a component instead is
refused by the service and by a trigger — the two shapes are mutually exclusive
by construction, not by rule.

The dish's first version runs a **closed** range on purpose, so the scenario shows
a **parent** supersession as well as a child one — two different corrections on
one screen. It is not a workaround: a child may be superseded under an open-ended
parent freely, because the parent's reference to it is a frozen foreign key.

Nothing invalid is ever seeded. A cycle or an over-deep chain appears only inside
a test that rolls back.
