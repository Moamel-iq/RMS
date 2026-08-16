# Task 0.7 — permissions, API, and idempotent integration

**Status: delivered.** The design was recorded before implementation; this
note and the marks below were added afterwards. Where the delivered system
differs from the plan, the plan text is kept and the difference is stated —
rewriting it to match would erase the decision.

See ADR-016 (permission and scope model) and ADR-017 (source identity and
idempotency) for the reasoning; this file is the task's checklist.

## 1. Domain permissions

Django's `add`/`change`/`delete` are not sufficient: they describe table
access, not accounting acts. "May post a journal" and "may edit a draft" are
different authorities over the same table.

| Permission | Grants |
|---|---|
| `accounting.view_journal` | Read entries and lines |
| `accounting.create_draft` | Create a draft entry |
| `accounting.edit_draft` | Amend a draft |
| `accounting.post_journal` | Post a draft to the ledger |
| `accounting.reverse_journal` | Reverse a posted entry |
| `accounting.manage_accounts` | Create and archive accounts |
| `accounting.manage_cost_centers` | Create and archive cost centres |
| `accounting.soft_close_period` | Move a period to `SOFT_CLOSED` |
| `accounting.close_period` | Move a period to `CLOSED` |
| `accounting.reopen_period` | Reopen a `CLOSED` period |
| `accounting.post_soft_closed_adjustment` | Post an adjustment into `SOFT_CLOSED` |
| `accounting.reverse_in_soft_closed_period` | Reverse into `SOFT_CLOSED` |

The last two require a non-empty reason and a recorded actor.

## 2. Organization and branch scoping

Permission alone is not authorization. A user holding
`accounting.post_journal` for one organization must not gain access by
submitting another organization's id.

- The organization and branch are resolved from the **caller's own
  memberships**, never taken at face value from the request body.
- A submitted `organization_id` or `branch_id` that the caller cannot reach is
  a 403, not a 404 and not a silent filter.
- Every list endpoint filters through `apps/organizations/selectors.py`.

## 3. API — commands, not CRUD

The ledger is not a table to be edited. Endpoints are named after the act.

```
POST   /api/v1/journal-entries/                 create a draft
PATCH  /api/v1/journal-entries/{id}/            amend a draft — draft only
POST   /api/v1/journal-entries/{id}/post/       post it
POST   /api/v1/journal-entries/{id}/reverse/    reverse it

POST   /api/v1/periods/{id}/soft-close/
POST   /api/v1/periods/{id}/close/
POST   /api/v1/periods/{id}/reopen/
```

The API layer does five things and no more: authenticate, authorize, validate
input shape, call the domain service, serialize the result. It must never
re-implement an accounting rule or write around the kernel.

## 4. Writable versus read-only

This table is a **constraint on how a resource may be exposed**, not a list
of endpoints to build. §3 above is the list of endpoints; every row here says
what shape an exposure must take *if* it exists, and what the admin may do.

| Resource | API | Django admin |
|---|---|---|
| Draft journal entry | Create, amend, delete — **built** (§3) | Read-only |
| Posted entry and lines | Command endpoints only — **built** (§3) | **Read-only** |
| Accounts, cost centres | Command endpoints only, if ever exposed — **none exposed in Release 1** | Read-only for normal admins — **built** |
| Periods | Command endpoints only — **built** (§3) | Read-only |
| Audit events | Read-only | Read-only (already) |

**No generic writable CRUD for posted accounting records anywhere.** Direct
ORM writes to posted ledger state are not an allowed application path.

> **Correction (Phase 2 gate).** The accounts-and-cost-centres row read
> "Command endpoints" with no qualifier, and §7 marked this whole section
> "Built". A reader — and `AccountAdmin`'s own docstring, which said "use the
> command endpoints" — could reasonably conclude those endpoints existed.
> They never did, and §3 never promised them: it enumerates seven routes,
> four for journal entries and three for periods, and `apps/accounting/api.py`
> ships exactly those plus the GET and draft DELETE this section authorises.
> What *was* delivered here is the read-only admin. Managing the chart is
> Phase 5 work (`docs/architecture/architecture-charter.md`), and its
> navigation section is deliberately inert. No endpoints were invented to
> make the old wording true; the wording is corrected to match what was
> approved and built. `manage_accounts` and `manage_cost_centers` therefore
> exist as permissions with no surface yet — they gate
> `services.create_account` / `create_cost_center`, which today only the
> chart seed command calls.

## 5. Deterministic idempotency for upstream modules

Purchases, Inventory, Sales, and Payroll will retry. The same economic event
must not be able to produce two journals.

Posting identity:

```
organization + source_type + source_id + source_event
```

for example `KM / PURCHASE_INVOICE / 145 / POSTED` — at most one posting, ever.

`JournalEntry.idempotency_key` already exists and is unique. The remaining
work is a derivation helper plus a `UniqueConstraint` over the four columns so
the guarantee does not depend on every caller composing the key identically.

**Built.** Delivered as a closed `SourceEvent` enum plus three constraints —
`journal_entry_source_event_is_known`,
`journal_entry_source_identity_complete_or_absent`, and the partial
`journal_entry_source_event_unique_per_organization`. The columns are named
`source_document_type` and `source_document_id` rather than
`source_type`/`source_id`, because `AuditEvent` already uses those names and
one vocabulary across the two is worth more than a shorter one here.

No derivation helper was written: deriving the key from the identity would
have made the two guarantees one, and they answer different questions — see
ADR-017, "Retry versus conflict".

## 6. Exact Decimal transport

API monetary values serialize as exact Decimal strings, never through binary
float, and preserve stored precision even where the normal UI shows whole IQD.
`apps/core/money.py::money_export` is the existing renderer for this.

**Built, in both directions.** Amounts also *arrive* as strings and are parsed
with `quantize_money`. Serializing exactly while accepting a JSON number would
have left the hole open at the point it actually matters: JSON has one numeric
type and it is binary floating point, so a bare `1250.001` in a request body
has already been through a float before any Python code sees it.

## 7. What was delivered

| Section | State | Notes |
|---|---|---|
| 1. Twelve domain permissions | Built | Declared on the models that own them; groups synced on `post_migrate`. A **thirteenth**, `manage_account_mappings`, arrived later with Task 1.3 — the tests assert thirteen |
| 2. Organization and branch scoping | Built | `OrganizationMembership` added — a period has no branch to be scoped to (ADR-016) |
| 3. Command API | Built | The seven routes listed in §3, plus `GET` list and detail, and `DELETE` for drafts per §4 |
| 4. Writable versus read-only | **Partly built** | The admin half is delivered: accounting admin is read-only for everyone, superusers included. **No account or cost-centre command endpoints exist**, and §3 never listed any — see the correction under §4. The constraint stands for whenever Phase 5 exposes them |
| 5. Idempotency | Built | See ADR-017 |
| 6. Exact Decimal transport | Built | Strings inbound and outbound |

Row 4 read a bare "Built" until the Phase 2 gate. A checklist that marks a
half-delivered section complete is worse than one that never mentioned it,
because it is the artefact a later reviewer trusts instead of reading the
code.

### Beyond the plan

Three things the plan did not anticipate, each forced by something found while
building:

- **`OrganizationMembership`.** The plan assumed branch memberships would
  carry every scope. A period spans every branch at once, so there was nothing
  for `reopen_period` to be scoped to. Branch authority deliberately does not
  accumulate into it (ADR-016).
- **A draft lifecycle in the kernel.** §3 lists create-draft and amend-draft
  endpoints, and Task 0.6 had no draft services — `post_entry` created a
  POSTED entry directly. `create_draft`, `update_draft`, `post_draft`, and
  `discard_draft` were added, along with a constraint trigger that checks
  balance at the DRAFT → POSTED transition: the 0002 balance trigger fires on
  journal *lines*, and promoting a draft touches none.
- **Drafts hold no entry number.** Journal numbering is gapless, so an
  abandoned draft must not burn one. The unique constraint became partial and
  a check constraint requires a number once the entry leaves draft.
