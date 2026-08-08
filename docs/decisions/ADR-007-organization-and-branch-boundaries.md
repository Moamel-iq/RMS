# ADR-007 — Organization and branch boundaries

- **Status:** Accepted
- **Date:** 2026-08-08
- **Related:** `docs/architecture/architecture-charter.md` §9, ADR-008

## Context

The charter is explicit that a `Branch` foreign key must not be added blindly
to every table, and that access must be enforced in services and queries
rather than left to the caller. What it did not pin down was where the
hierarchy stops for Phase 0 and how a user is attached to a branch.

## Decision

**Hierarchy.** `Organization` is the top boundary. `Branch` belongs to exactly
one organization. Warehouse, kitchen location, cash point, and cost centre are
*not* modelled here — the installation plan assigns them to their owning
modules, and inventing them now would fix their shape before Inventory has
requirements.

**Membership is a relationship, not a field.** A user may hold access to
several branches, so `User.branch` cannot exist. Access is a
`BranchMembership` row carrying user, branch, and role. This is why Task 0.2
deliberately shipped no branch field on `User`.

**Uniqueness is scoped, not global.** Branch codes are unique *within* an
organization. Two organizations may each run a branch coded `BUNOOK`. The
constraint is `UNIQUE (organization, code)` in the database.

**Bilingual names are stored, not translated.** `name_ar` and `name_en` are
data — a branch is named by the business, not by gettext. This differs from
interface strings, which are translated (ADR-011).

**Deactivation, never deletion.** Organizations and branches carry `is_active`
and are protected against cascade deletion. A branch with posted ledger
history cannot be removed without destroying the audit trail, so the model
refuses at the database level (`on_delete=PROTECT`).

**Superusers see every branch.** Made explicit and tested rather than left as
an accident of the ORM. A Django superuser is already unrestricted; pretending
otherwise in the selector would be theatre.

## Alternatives considered

- **A single `branch` field on `User`** — simpler, and wrong the first time an
  accountant covers two branches. Retrofitting it later means rewriting every
  query that assumed one branch.
- **Global branch codes** — would force `BUNOOK-1`, `BUNOOK-2` naming across
  organizations and leak one tenant's structure into another's namespace.
- **Django Groups for roles** — flexible, but a group name carries no
  organization or branch scope, so "Manager" would mean manager everywhere.
  Roles live on the membership row instead.

## Consequences

- Every selector that reads branch-owned data must filter by the caller's
  accessible branches. `apps/organizations/selectors.py` is the single place
  that answers "which branches may this user see".
- Cross-branch operations (inter-branch transfers) will need access to both
  source and destination and must check both. Nothing here grants that
  implicitly.
- Adding warehouses in Phase 1 means adding a second scoping level. The
  selector API is written to be extended, not replaced.

## Open

Role names are taken from the separation-of-duties examples in the charter
(storekeeper, purchasing, accountant, cashier, manager, finance). They are not
sourced from an SRS, because none exists. Approval thresholds and the
"no user approves their own high-risk transaction" rule are **not** enforced
yet; the state model leaves room for them.
