# ADR-016 — Permission and scope model

- **Status:** Accepted. **Implemented by Task 0.7.**
- **Date:** 2026-08-09
- **Related:** ADR-007 (organization and branch boundaries), ADR-013 (periods),
  ADR-015 (cost centers)

## Decision

Authorization is two independent facts, checked together and never conflated:

```
Role  ->  Permission  ->  Scope  ->  Application service  ->  Accounting kernel
```

**A permission says what.** Named after accounting acts, not table access.
Django's `add`/`change`/`delete` describe rows; "may amend a draft" and "may
post it to the ledger" are different authorities over the same table, and a
ledger where they are one permission has no separation of duties at all.

**A scope says where.** Resolved from the caller's own memberships, never from
the request. This is the rule the whole model exists for:

> A user authorized for one organization or branch must not gain access merely
> by submitting another `organization_id` or `branch_id`.

### Roles are Django groups

One group per role, named `role:<ROLE>`. A membership — at a branch or over an
organization — puts the user in that role's group; the group carries the
permissions. Modules contribute their own permissions into the same groups, so
a role's authority grows as modules arrive without any central file having to
know about them.

Group membership is **recomputed** from the memberships that exist, never
incremented on grant and decremented on revoke. The increment/decrement pair
silently strips authority from someone who holds the same role in two places
and loses only one of them.

### Two scopes, and the containment runs one way

| Scope | Held through | Reaches |
|---|---|---|
| Branch | `BranchMembership` | that branch |
| Organization | `OrganizationMembership` | the organization, and **every branch in it** |

**Organization authority contains its branches.** An Accounting Manager who
could reopen a period covering every branch, yet post no adjustment into any
of them, would hold an authority with no way to exercise it.

**Branch authority never adds up to organization authority.** Holding a role
at all three branches today is not organization scope, because a fourth branch
opening tomorrow would silently revoke it — an authority change no audit could
explain and nobody decided.

### Which scope each permission uses

| Scope | Permissions |
|---|---|
| Branch | `view_journal`, `create_draft`, `edit_draft`, `post_journal`, `reverse_journal` |
| Organization | `manage_accounts`, `manage_cost_centers`, `soft_close_period`, `close_period`, `reopen_period`, `post_soft_closed_adjustment`, `reverse_in_soft_closed_period` |

A journal line names a branch, so posting is answered per branch — and at
**every** branch the entry touches, because authority over half an entry
authorizes nothing. A period, a chart of accounts, and a cost centre belong to
the organization, so one branch must not reshape what the others post to.

### Resolution, not verification

`resolve_branch(user, branch_id)` takes the caller and the id together and
returns an object only if the caller reaches it. Nothing fetches an object by
id and checks it afterwards, because the intermediate state — an out-of-scope
object sitting in a local variable — is the shape of the bug. There is no
moment at which the wrong object exists and is waiting to be used.

### Authorization lives above the kernel

`apps/accounting/services.py` knows accounting and nothing about users.
`apps/accounting/commands.py` resolves scope, checks permissions, binds the
audit actor, and calls the kernel. The API calls commands and never the
kernel, enforced by a test that parses the imports.

The layering also makes the audited actor the authorized actor: every kernel
call runs inside `audit_context(actor=...)` with the user the permission was
checked against, so "who was allowed" and "who is recorded" cannot disagree.

## Alternatives considered

- **Roles checked directly in the services** — `if role == "ACCOUNTANT"`. Reads
  clearly and hard-codes an org chart into accounting logic. Renaming a role,
  splitting one, or granting an exception then means editing posting code.
- **Django's model permissions alone.** Free, and they describe table access.
  There is no `change_journalentry` that means "post it" as distinct from
  "amend the draft", so a permission that granted one would grant both.
- **Branch memberships only, with organization authority derived** as "holds
  the role at every branch". No new model, and it makes opening a branch a
  silent revocation of somebody's authority.
- **Object-level permissions (django-guardian).** A row per user per object.
  Correct in general and far more machinery than a two-level hierarchy needs;
  the scope here is structural, not per-record.
- **403 for out-of-scope objects** instead of 404. Tells an honest client
  plainly that their authority is insufficient, and tells a dishonest one that
  the record is real. **Rejected — see the amendment below.**

## Consequences

- A permission can only be **narrowed** by scope, never widened by it. Holding
  a group's permission is necessary and never sufficient.
- A superuser satisfies both halves rather than skipping them, so emergency
  authority reaches the same services with the same reason, ordering, and
  audit requirements. It changes who may ask, not what is checked.
- New modules add permissions to the existing role groups. They do not invent
  a parallel scoping mechanism, and `apps/organizations/authorization.py` is
  the only place that answers "may this user act here".
- `role_at_branch` still answers only about branch memberships. An
  organization member holds no branch *post*, which is the honest answer;
  authorization does not use it.

## Amendment — role defaults (approved 2026-08-09, Task 0.8)

| Role | Accounting permissions by default |
|---|---|
| OWNER | all twelve |
| ACCOUNTING_MANAGER | all twelve |
| ACCOUNTANT | `view_journal`, `create_draft`, `edit_draft`, `post_journal`, `reverse_journal` |
| MANAGER, PURCHASING, VIEWER | `view_journal` |
| CASHIER, STOREKEEPER | none |

**An accountant records transactions and decides nothing structural.** The
shape of the chart, the managerial dimensions, and when a period stops
accepting entries are organization-level decisions that affect every branch at
once — `manage_accounts`, `manage_cost_centers`, and all five period-and-
override permissions are ACCOUNTING_MANAGER / OWNER authority.

These are **defaults, not kernel rules**. A deployment wanting a senior
accountant to hold more grants it deliberately; no accounting code changes.

**OWNER means the trusted organization proprietor** — the person accountable
for the books. A passive investor or shareholder must not hold this role: their
legitimate interest is disclosure, and OWNER carries the authority to post,
reverse, close, and reopen. A read-only reporting role for shareholders is a
real future need and deliberately does **not** exist yet; when it arrives it
must be its own role rather than a relabelling of this one.

## Amendment — out of scope is 404, not 403 (approved 2026-08-09, Task 0.8)

The original decision answered 403 for anything the caller could not reach.
That is reversed. Two distinct answers now:

| Situation | Answer | Exception |
|---|---|---|
| Outside the caller's organization or branch scope | **404** | `OutOfScope(ObjectDoesNotExist)` |
| Inside their scope, lacking authority for this act | **403** | `PermissionMissing(PermissionDenied)` |

**Why the reversal.** A 403 about another organization's record confirms that
the record is real. Ids are sequential, so a caller who can enumerate them and
read the status code obtains a census of a competitor's journals, invoice
numbers, accounts, and cost centres without ever reading a field. The status
code was the leak.

Outside a caller's tenancy, a record does not exist *as far as they are
concerned*, and the API says exactly that — same code, same wording, whether
the row is absent or simply not theirs. Anything less makes the two
distinguishable and the fix cosmetic.

**Reaching is weaker than scope.** `require_organization_permission` answers
404 only when the caller cannot reach the organization at all — no membership
in it and no branch of it. A branch accountant asking to close a period in
their *own* organization gets 403: the period is not foreign to them, they
simply may not seal it for every other branch. Reaching decides whether they
may be told "no"; it never grants anything.

The cost is the one the original decision named: an honest client who has
genuinely lost access sees 404 and may look for a record that exists. That is
accepted. Confirming a competitor's data exists is the worse failure.
## Amendment — permission provenance (approved 2026-08-09, Task 1.1 completion)

The two halves must come from the **same place**. It is not enough that the
caller holds the permission *somewhere* and reaches the target: the permission
must be carried by a role they hold **inside the target organization, branch,
or warehouse**.

**Why this had to be stated.** Role groups are recomputed from *every*
membership a user holds, so `user.has_perm("inventory.manage_items")` is true
the moment they manage any organization at all. Reach, meanwhile, is cheap —
a read-only viewer post at one branch grants it. Combining the two therefore
let a manager at Khan Mandi who also held a viewer post at a rival rewrite the
rival's item master. The scope check was present and correct; the permission
half was simply answered from the wrong place.

Two acceptable sources of a permission over an organization:

1. an `OrganizationMembership` in **that** organization whose role grants it;
2. a `BranchMembership` in a branch of **that** organization whose role grants
   it.

And, correspondingly:

| Question | Roles that may answer it |
|---|---|
| Organization *authority* (`post_opening_stock`, period acts) | the `OrganizationMembership` role in that organization, and nothing else |
| Organization *master data* (item master, categories, packages, conversions) | any role held inside that organization, branch posts included |
| Branch | the `BranchMembership` role at that branch, plus organization-wide roles over its owner |
| Warehouse | the roles of memberships that actually cover that warehouse — a `SELECTED` membership that omits it contributes nothing |

`roles_granting(permission)` reads the role groups themselves rather than
importing each module's role map, so it stays correct as modules land and
`apps.organizations` never has to know what `apps.inventory` grants.

**Consequences, stated plainly.**

- A permission attached directly to a user, or through a group outside the
  `role:` namespace, authorizes **no** organization. Such a grant names no
  post, so it cannot say *where* it applies. Widening authority is still
  entirely possible — change the role map, or give the person the role — but
  it is done somewhere that records where the authority reaches.
- A superuser holds no membership, so provenance is short-circuited for them
  rather than failed. Consistent with the rest of this ADR: emergency
  authority changes who may ask, never what the service checks.
- Screens gate their buttons with `organizations_with_permission` /
  `branches_with_permission`, which answer the same question in bulk. A test
  pins the bulk answer to the single-object answer, because a screen that
  offered a button the write would refuse — or hid one it would allow — is a
  worse defect than either alone.
