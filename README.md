# Khan Mandi RMS

Restaurant Management System for Khan Mandi — Al-Bunook Branch, Baghdad, Iraq.

Django 5.2 LTS · Python 3.14 · PostgreSQL 18 · Django Ninja.

Project policy for AI-assisted work lives in [CLAUDE.md](CLAUDE.md).
Architecture decisions live in [docs/decisions/](docs/decisions/README.md).

## Status

Phase 0 (Foundations), Task 0.4 complete — custom `User` model,
phone/username authentication, the Arabic RTL sign-in screen, the
organization/branch hierarchy with branch-scoped access, the application
shell, and units of measure with the quantity precision policy (ADR-006).

Next: Task 0.5, the audit foundation. Task 0.6 (accounting kernel) is
**blocked** on the monetary precision decision — see
[docs/decisions/README.md](docs/decisions/README.md).

## Setup from a fresh clone

Requires Python 3.14 and a running PostgreSQL 18.

1. Create the database role and database (as the `postgres` superuser):

   ```sql
   CREATE ROLE khan_mandi_dev WITH LOGIN CREATEDB NOSUPERUSER NOCREATEROLE;
   \password khan_mandi_dev
   CREATE DATABASE khan_mandi_dev WITH OWNER = khan_mandi_dev ENCODING = 'UTF8';
   ```

2. Create the virtual environment and install dependencies:

   ```
   py -3.14 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install "pip==26.1.2" setuptools wheel pip-tools
   .\.venv\Scripts\pip-sync.exe requirements-dev.txt
   ```

   pip is pinned deliberately — pip 26.2+ breaks pip-tools. See ADR-010.

3. Copy `.env.example` to `.env` and fill in the real values. Generate a secret
   key with:

   ```
   .\.venv\Scripts\python.exe -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())"
   ```

   `.env` is gitignored and must never be committed.

4. Install the git hooks:

   ```
   .\.venv\Scripts\pre-commit.exe install
   ```

5. Verify:

   ```
   .\.venv\Scripts\python.exe manage.py check
   .\.venv\Scripts\pytest.exe
   ```

## Quality gate

All of these must pass before any commit:

```
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\ruff.exe check .
.\.venv\Scripts\ruff.exe format --check .
.\.venv\Scripts\mypy.exe apps config tests
.\.venv\Scripts\pytest.exe --cov=apps --cov=config
.\.venv\Scripts\pre-commit.exe run --all-files
```

## Layout

```
apps/        foundation and domain apps (empty until Task 0.2)
config/      settings split, root URLs, Django Ninja API
docs/        architecture, decisions, plans, requirements, specs
locale/      Arabic and English translations
scripts/     repository tooling
tests/       project-level tests
```
