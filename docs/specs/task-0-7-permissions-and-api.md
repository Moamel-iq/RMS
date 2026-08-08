# Task 0.7 — permissions, API, and idempotent integration

The approved design, recorded before implementation so it is not carried in a
conversation. Nothing here is built yet unless a line says otherwise.

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

| Resource | API | Django admin |
|---|---|---|
| Draft journal entry | Create, amend, delete | Read-only |
| Posted entry and lines | Command endpoints only | **Read-only** |
| Accounts, cost centres | Command endpoints | Read-only for normal admins |
| Periods | Command endpoints only | Read-only |
| Audit events | Read-only | Read-only (already) |

**No generic writable CRUD for posted accounting records anywhere.** Direct
ORM writes to posted ledger state are not an allowed application path.

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

**Not yet built.** Required before Phase 0 exit.

## 6. Exact Decimal transport

API monetary values serialize as exact Decimal strings, never through binary
float, and preserve stored precision even where the normal UI shows whole IQD.
`apps/core/money.py::money_export` is the existing renderer for this.
