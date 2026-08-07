# ADR-002 — PostgreSQL 18 as the only database

- **Status:** Accepted
- **Date:** 2026-08-08
- **Related:** `docs/plans/installation-to-coding-start-plan.txt` §2, §7

## Context

The financial invariants of this system — balanced journal entries, prohibited
negative stock, scoped uniqueness, period locks — are to be enforced by
database `CHECK` and `UNIQUE` constraints, not by Python validation alone.
That is only meaningful if development, CI, and production all run the same
engine.

## Decision

PostgreSQL 18 everywhere. SQLite is never permitted, including for convenience
during development or for a fast test run.

Local: PostgreSQL 18.4, role `khan_mandi_dev` (LOGIN, CREATEDB, NOSUPERUSER,
NOCREATEROLE), database `khan_mandi_dev`, UTF-8. `CREATEDB` is granted because
Django creates a temporary test database; a production application role must
not receive it.

A test asserts the configured engine is `django.db.backends.postgresql` and
that no `*.sqlite3` file exists in the repository
(`tests/test_settings.py::TestDatabaseConfiguration`).

## Alternatives considered

- **SQLite for tests** — faster, but silently drops the constraint semantics
  the correctness argument depends on. Rejected.
- **PostgreSQL 19** — in beta. Rejected for a financial system.

## Consequences

- Contributors must run a local PostgreSQL 18 instance; there is no zero-setup
  path.
- CI runs a `postgres:18` service container.
- Collation and encoding are set at database creation time. Changing them later
  requires a dump and reload, so they must be confirmed identical between
  development and production before real data exists. **Open:** the production
  database must be created with an ICU locale for correct Arabic sorting; the
  local development database currently uses the installer default.
