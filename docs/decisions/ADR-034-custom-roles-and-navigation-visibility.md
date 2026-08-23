# ADR-034 — Custom roles, and a navigation that shows only what the reader may do

- **Status:** Accepted.
- **Date:** 2026-08-23
- **Related:** ADR-016 (permission and scope model), ADR-007 (organization and
  branch boundaries)

## Context

ADR-016 settled that authorization is a permission (*what*) held through a
role, exercised inside a scope (*where*). It shipped eight roles as a fixed
vocabulary — owner, accounting manager, manager, accountant, purchasing,
storekeeper, cashier, viewer — each a Django group whose permissions every
module writes at start-up.

The owner now asks for two things the fixed vocabulary cannot give:

1. **A post they define.** "An accountant I configure": which screens that
   person sees, what they may create, what they may edit. The eight roles are
   the charter's separation-of-duties posts; a restaurant also has the person
   who only reads reports, the clerk who registers items but must not change
   one, and whatever post next year invents.
2. **A navigation that hides what a person cannot do.** Today every module and
   every section is drawn for everyone and the view answers 403. For the owner
   that is "a part of the system I cannot hide from an employee".

And one example that exposed a lump in the vocabulary: `inventory.manage_items`
covered creating, editing and archiving an item, so "may add but not edit" was
not expressible.

## Decision

### 1. A custom role is an organization-defined permission set, granted like any role

`RoleDefinition(organization, code, name_ar, permissions, is_active)` lives in
`apps.organizations`. Its membership key is `custom:<organization_id>:<code>`
and its group is `role:custom:<organization_id>:<code>` — **the same
machinery ADR-016 built**, not a second one:

- A membership stores the key in its `role` column exactly as it stores
  `ACCOUNTANT`. Branch and organization memberships both accept it.
- The group carries the definition's permissions; `sync_user_role_groups`
  recomputes the user's groups from memberships as before.
- `roles_granting(permission)` reads the `role:` groups, so provenance holds:
  the permission must come from a role held **inside** the target
  organization, and a custom role can only ever be held inside the
  organization that defined it, because the key names that organization.

The eight built-in roles remain, unchanged, as the charter's posts and as
templates: the roles screen shows each with its effective permissions, and a
custom role can be started as a copy of one.

A custom role that has active memberships cannot be archived; the memberships
are revoked first, deliberately, one by one. Archiving a role must not be a
way to strip authority from people without a record per person.

The `role` column loses its `choices`. Validation moves to the grant services,
where it belongs: a key is valid if it names a built-in role or an active
definition of the **target** organization. A definition of organization A
granted at a branch of organization B is refused — the key would name a group
whose permissions A decided.

### 2. Permissions are the unit the owner configures — with Arabic names

The owner configures a role by ticking permissions. Each permission keeps its
codename (the contract every service checks) and gains an Arabic label and a
place in a catalogue grouped by module and section, in
`apps/organizations/permission_catalog.py`. The catalogue is presentation: it
decides how the matrix reads, never what a permission does. A permission
missing from the catalogue is still a permission; it is listed under its
module without a section.

Permissions are named after acts (ADR-016), and an act the owner wants to grant
separately must be its own permission. `manage_items` is therefore split:
`create_item` (register a new item), `edit_item` (change one), and
`manage_items` keeps the structural acts — archive, reactivate, correct a base
unit. Built-in roles that held the lump hold all three, so nobody's authority
changes by this split; only the vocabulary becomes fine enough to express the
owner's example. Other lumps are split the same way when a deployment asks.

### 3. Navigation shows what the reader may do

Each section's required permission is **derived from the view it links to** —
`resolve(reverse(url_name)).func.view_class.required_permission` — rather than
declared a second time in the navigation registry. The registry would drift
from the views; the views are the truth about what a screen needs. The shell
context processor filters the registry per user:

- a section is shown if the user holds its permission (superusers hold all);
- a module is shown if it has one visible section, and it opens on its first
  visible section when the user may not open its own landing page;
- the settings module is shown to staff, as its views already require;
- the home module is always shown.

Hidden, not muted. ADR-016 already answers 403 for a URL typed by hand; the
navigation is a courtesy to the reader, not the gate. Unbuilt sections keep
their muted rendering — that is a statement about the system, not about the
reader.

## Alternatives considered

- **Editing the built-in roles' permission sets per organization.** Gives the
  owner control over "accountant" but not a *new* post, and it would make
  `ACCOUNTANT` mean different things in different organizations while the
  charter's separation-of-duties text names it as one thing.
- **Per-user permissions.** Django supports them; ADR-016 deliberately
  refuses them, because a permission attached to a user names no post and so
  says nothing about where it applies. A custom role is a post.
- **Declaring a permission on every navigation entry.** One more list to keep
  in step with the views. Deriving it from the view cannot drift.
- **Object-level grants (django-guardian).** Still more machinery than a
  two-level hierarchy needs; the owner's asks are all at the level of "this
  screen" and "this act".

## Consequences

- `Role` is no longer the closed set of valid role keys. Code that needs a
  label uses `role_label(key)`; code that needs "is this a built-in" asks
  `Role` directly. `get_role_display()` is gone with the `choices`.
- `roles_in_organization` and friends return custom keys alongside built-in
  ones; every consumer already treats them as opaque strings.
- A module's `sync_role_groups` touches only the `role:<BUILTIN>` groups it
  always did. Custom groups are written by `sync_role_definition_group` and by
  nothing else.
- Hiding is per permission, so a screen that exists but whose view declares no
  permission stays visible to everyone signed in. That is the honest default:
  a screen nobody gated is a screen nobody meant to hide.
- The demo seed creates one DEMO custom role — a reports-only reader — so the
  roles screen and the hidden navigation can be exercised without inventing an
  employee.
