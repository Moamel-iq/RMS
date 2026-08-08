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
- **404 for out-of-scope objects** instead of 403. Hides existence, and sends
  an honest client hunting for a record that is there. A caller who probes
  learns the same thing after two requests either way. See Consequences.

## Consequences

- A permission can only be **narrowed** by scope, never widened by it. Holding
  a group's permission is necessary and never sufficient.
- A superuser satisfies both halves rather than skipping them, so emergency
  authority reaches the same services with the same reason, ordering, and
  audit requirements. It changes who may ask, not what is checked.
- New modules add permissions to the existing role groups. They do not invent
  a parallel scoping mechanism, and `apps/organizations/authorization.py` is
  the only place that answers "may this user act here".
- Out-of-scope access returns **403 and not 404**, which confirms a record
  exists to a caller who cannot read it. Accepted deliberately: this is an
  internal ERP where the alternative sends honest clients into retry loops.
- `role_at_branch` still answers only about branch memberships. An
  organization member holds no branch *post*, which is the honest answer;
  authorization does not use it.
