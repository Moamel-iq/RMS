# ADR-019 — Account roles and domain-owned posting mappings

- **Status:** **Accepted** (2026-08-09, Task 1.3)
- **Date:** 2026-08-09
- **Related:** ADR-014 (chart of accounts), ADR-016 (permission and scope),
  ADR-017 (source identity), ADR-018 (stock ledger)
- **Detail:** `docs/tasks/task-1-0-inventory-domain-spec.md` §11

This is the durable architecture for how *every* module — Inventory now,
Purchases, Sales, and Payroll later — decides which GL account a posting
lands in. It will be referenced long after Task 1.3.

## Decision

### 1. Posting rules speak in roles, never in accounts

A posting service refers to an `AccountRole` code — `INVENTORY_CONTROL`,
`INVENTORY_OPENING_EQUITY` — and never to an account primary key or account
code. Which *account* carries a role is an organization's decision, recorded
as data. A hard-coded account id anywhere in a posting rule is a defect: it
welds one deployment's chart into every deployment's code.

`AccountRole` is a **global, system-owned vocabulary**, seeded by migration
and re-asserted on `post_migrate`. System codes are technical identifiers:
locale-independent, never renamed, never deleted — a database trigger refuses
both, because renaming a role would silently orphan every mapping and every
snapshot that names it. Release 1 seeds exactly the seven approved inventory
roles and nothing speculative; only `INVENTORY_CONTROL` permits per-item
overrides (`mapping_scope=ITEM`), because it is the one role whose account
carries standing value.

### 2. The dependency direction is one-way: domain → accounting

```
apps.accounting owns:            apps.inventory owns:
  AccountRole                      InventoryAccountMapping
  OrganizationAccountMapping         (item XOR category overrides)
  resolve_default_account            resolve_inventory_account
                                     (the resolver orchestration)

        inventory ──imports──▶ accounting          NEVER the reverse
```

Accounting must not import `InventoryItem` or `ItemCategory` — the chart is
organization property that predates and outlives any one module, and a chart
kernel that knew about items would soon know about invoices, payslips, and
menu prices. The overrides live in inventory *because* they reference item
concepts; the defaults live in accounting *because* every module resolves
through them.

Where accounting genuinely needs a domain's veto — the reclassification guard
in §5 — it exposes a **registration hook** (`register_mapping_guard`) that
domains attach to at app-ready. Inversion of control instead of an import:
accounting calls a function it was handed, and never learns what an item is.

### 3. Mappings are effective-dated versions, and used means immutable

`organization + role + effective range → postable account`, versioned per
`(organization, role)`, with a PostgreSQL EXCLUDE constraint making
overlapping active ranges unrepresentable. Resolution is exact by date and
**never guesses**: a missing mapping raises `account_role_unmapped` before
any effect exists. There is no fallback account, because a figure in the
wrong account is worse than no figure — it reconciles.

A mapping that a posting has snapshotted cannot be edited or archived. The
correction is closing its effective range and creating a new version; the old
row stays readable forever, which is what keeps "which account did this
movement post to" answerable after the chart changes. Usage is detected
generically over reverse relations, so a referencing model added by a later
module is protected without accounting knowing its name.

### 4. Resolution precedence, and the snapshot rule

    1. exact item mapping
    2. nearest category ancestor, leaf towards root
    3. organization default
    4. account_role_unmapped

Resolution happens **at posting** and the chosen mapping, account, journal
entry, and journal line are written onto the document line, immutably, inside
the posting transaction. Nothing ever re-resolves a historical effect through
today's mapping — reconciliation groups history by the account it actually
entered, which is the only comparison that can catch drift.

### 5. Standing stock value cannot be re-homed silently

Changing an `INVENTORY_CONTROL` mapping — override or organization default —
is refused with `inventory_account_reclassification_required` whenever it
would change the resolved account of an item with non-zero stock. So is
moving such an item to a category that resolves differently. The standing
value sits in account A; every new posting would go to account B; no journal
ever moved the money, and the trial balance would drift by exactly the value
nobody reclassified. Until an explicit GL reclassification workflow exists,
the door is closed. Historical movements and journals keep their original
accounts regardless.

The guard is apply-then-verify inside the mutating transaction: capture the
resolution before, apply, re-resolve, and roll the whole change back on any
difference — one enforcement for every path, including accounting's own
screens via the hook in §2.

### 6. Authority

`accounting.manage_account_mappings`, ORGANIZATION scope, held by default by
OWNER and ACCOUNTING_MANAGER only. It gates the defaults **and** the
inventory overrides: where an item's value posts is one decision, whichever
table records it. Provenance rules from ADR-016 apply unchanged — the
permission must be carried by an `OrganizationMembership` role in the target
organization; global grants and branch accumulation authorize nothing.

## The combined posting order (first exercised by opening stock)

Task 1.3's opening post is the first code path writing both ledgers in one
transaction. The global lock order every combined service must use:

    1. the source document row              (select_for_update)
    2. stock keys, canonical order          (advisory locks)
    3. the stock posted-order counter       (inside post_stock_entry)
    4. the domain document-number sequence
    5. the journal-number sequence          (inside post_entry)

Steps 3 and 4 are deliberately swapped relative to the Task 1.3 brief's
suggested order: the stock kernel owns "keys then counter" as one unit, and
splitting that unit to interleave a document number would scatter the
kernel's locking discipline across modules. What matters — and what the
concurrency tests hold — is that the order is documented, globally
consistent, and that **no official code path posts the accounting journal
first and then asks for inventory locks.**

## Alternatives considered

- **An `inventory_account` FK on the item.** A second resolution path that
  competes with the mapping silently; rejected in Task 1.0 and stays
  rejected.
- **Mappings inside each domain, defaults included.** Purchases and Payroll
  would each grow their own default table, and "which account carries
  opening equity" would have three answers.
- **Accounting owning the overrides too.** Requires accounting to import
  item and category models, and every future domain's models after them —
  the reverse dependency this ADR exists to forbid.
- **Re-resolving history through current mappings for reconciliation.**
  Makes the report agree with the chart instead of with history; the drift
  it exists to catch becomes invisible.
- **Allowing mapping changes over standing stock with a warning.** A warning
  is a decision deferred to whoever ignores it. The refusal stands until a
  real reclassification document exists to move the money visibly.

## Consequences

- Purchases, Sales, and Payroll add roles (`GRNI`, `SALES_REVENUE`, …) and
  reuse the resolver unchanged; each keeps any domain-specific overrides in
  its own app.
- A new organization must map its roles before its first opening posts —
  `account_role_unmapped` is the intended first-run experience, not a crash.
- The mapping screens are native: organization defaults under Accounting,
  item/category overrides under Inventory, both gated by the same authority.
- Enabling FIFO later, or any strategy that consumes layers, changes nothing
  here: roles and mappings are about *where* value posts, not how it is
  measured.

## Amendments applied at Task 1.4 (2026-08-10)

### 5. The reclassification guard needs a lock, not only a comparison

The guard in §5 above compares the account resolution before and after a
mapping change and refuses any difference over standing stock. Task 1.4 asked
whether that alone closes the race against a concurrent posting. **It does
not**, and the gap is real: the guard can only see *committed* stock, so a
posting that had already read the old mapping and not yet committed is
invisible to it. The mutation would commit, the posting would commit, and
standing stock would sit in one account while every later posting used
another — with nothing left to detect it.

So the comparison is now wrapped in a lock, per organization:

| Operation | Mode |
|---|---|
| Any stock posting that resolves an account | `pg_advisory_xact_lock_shared` |
| Creating, amending, closing, or archiving a mapping | `pg_advisory_xact_lock` |
| Moving an item between categories | `pg_advisory_xact_lock` |

Shared, so concurrent postings do not serialise against each other — the cost
is paid only by the rare mutation, which waits for the in-flight postings and
blocks the next. The helpers live in `apps/core/locks.py`, beneath both
`apps.accounting` and `apps.inventory`, because both take them and neither may
import the other.

**A mutation must take the advisory lock before its `SELECT ... FOR UPDATE`,
and the order is not cosmetic.** A posting holds the shared lock and then
writes a document line carrying `resolved_organization_mapping`, which needs a
KEY SHARE lock on the mapping row. A mutation that took the row first and only
then asked for the advisory lock would hold exactly what the posting waits for
while waiting for exactly what the posting holds. That deadlock was observed —
PostgreSQL detected and killed it — in the first run of the Task 1.4
concurrency tests, and `begin_mapping_mutation` is the fix.

### 6. The global lock order, extended

Every combined service acquires in this order, and no official code path may
invert it:

    1. the source document row                   select_for_update
    2. the organization's account-mapping lock   shared for postings,
                                                 exclusive for mutations
    3. source rows a document depends on         e.g. the issue lines a
                                                 return goes back against
    4. the stock keys, sorted canonically        advisory
    5. the inventory posted-sequence counter
    6. the domain document-number sequence
    7. the journal-number sequence

Steps 5 and 6 stay in that order because the stock kernel owns "keys then
counter" as one unit; splitting it to interleave a document number would
scatter the kernel's locking discipline across modules.

### 7. Control-account continuity lives on the movement

§4 says a posting snapshots the mapping it resolved. Task 1.4 makes the
*account* itself a column on the effect:

- **`StockMovement.control_account`** — the inventory-control account this
  movement's value entered or left, captured at posting and immutable with the
  rest of the row.
- **`StockBalance.control_account`** — the account the position's current
  stock cycle belongs to, cleared when the position empties.

Three rules follow, and the third is the one that matters:

1. An inbound into an **empty** position establishes the account. Nothing is
   stranded, so a mapping changed since the position last emptied takes effect
   exactly where it should.
2. An inbound into **standing stock** must use the account already there, or
   it is refused with `inventory_account_reclassification_required`. Blending
   two accounts into one position would put its value in two places with no
   journal between them.
3. An **outbound leaves through the account it entered**, read from the
   balance and never resolved afresh. This is what stops a consumption issue
   crediting stock out of an account it was never in after the mapping moved.

The column is nullable, and the null means something specific: a movement
posted with no mapping in play at all — the bare kernel, exercised by its own
tests. Every movement a business document posts carries one, and a test holds
that line. Inventing an account for a posting that resolved none would be
worse than recording that it had none, so this is deliberately a service
invariant with a test rather than a `NOT NULL` that would force a fiction.

Reconciliation now reads this column directly instead of walking from each
movement to whichever document produced it — which also means a document type
added later is attributed correctly without the reconciler learning about it.

### 8. Two roles added, and why their scopes differ

| Role | Scope | Reason |
|---|---|---|
| `GOODS_RECEIVED_NOT_INVOICED` | Organization default only | A clearing liability waiting for an invoice. Which item arrived says nothing about who is owed for it. |
| `INVENTORY_CONSUMPTION` | Item/category override permitted | What a thing is consumed *as* is a property of the thing. Packaging, cleaning materials, and direct ingredients belong in different expense accounts, and one organization-wide answer would make the consumption figures useless for costing. |

Seeding a role is not seeding a mapping. Posting fails with
`account_role_unmapped` until an accounting manager maps them deliberately.
