# ADR-001 — Django 5.2 LTS on Python 3.14

- **Status:** Accepted
- **Date:** 2026-08-08
- **Related:** `docs/plans/installation-to-coding-start-plan.txt` §2

## Context

The system is a financial ERP with a multi-year life. The framework must be
under long-term support for the whole of the initial build, and the interpreter
must be supported by both the current LTS and the next one, so the eventual
LTS-to-LTS jump does not also force an interpreter migration.

## Decision

Django 5.2 LTS on Python 3.14, PostgreSQL 18.

`requirements.in` pins `Django~=5.2.8`, not `~=5.2.0`. Python 3.14 support was
added to the 5.2 series in **5.2.8**; a resolution below that floor would
install a Django that does not support the interpreter in use. Resolved to
Django 5.2.17.

## Alternatives considered

- **Django 6.0** — a standard release, supported only to April 2027. Shorter
  window than 5.2 LTS despite being newer.
- **Django 6.2 LTS** — the next LTS, expected April 2027. Does not exist yet.
- **Python 3.13** — proposed by `docs/plans/environment-setup-wsl-alternative.md`.
  Fully viable, but 3.14 is supported by 5.2.8+ and by the coming 6.2 LTS, so
  it avoids a second migration.

## Consequences

- Django 5.2 is supported to **30 April 2028**. The 5.2 → 6.2 upgrade must be
  budgeted explicitly once 6.2 ships; it is not a multi-year-stable resting
  point.
- The `~=5.2.8` floor must not be relaxed.
- Any dependency that lags on Python 3.14 blocks the build. This was checked at
  bootstrap: Django, django-ninja, psycopg 3, pytest, ruff, mypy, django-stubs,
  and pre-commit all install and run cleanly.
