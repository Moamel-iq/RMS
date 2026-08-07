# Environment Setup & Bootstrap Plan — Khan Mandi Restaurant ERP (Django, Windows → Cloud)

## TL;DR
- **Build on WSL2 (Ubuntu) from day one**, on Django **5.2 LTS** + Python **3.13** + PostgreSQL **18**, managed with **uv**, with data services in Docker and Django running natively in WSL. This gives Linux/cloud parity, working Celery, fast inotify file-watching, and clean Playwright/Chromium behavior — the four things native Windows breaks.
- **Validate Arabic PDF rendering with Playwright/Chromium in the first days, not at the end.** Chromium's print engine is the only option that gets Arabic shaping and bidi right out of the box; WeasyPrint's own docs list bidirectional text as unsupported, and ReportLab's RTL support is still experimental. Lock in the irreversible decisions now: PITR backups, `Asia/Baghdad` timezone, UTF-8 encoding, and an ICU collation for Arabic sorting.
- **Configure Claude Code's permission deny-list before the first prompt** so it can never drop the database, fake/reset migrations, or force-push, then work spec→test→implement→review→commit with plan mode, hooks, and a read-only Postgres MCP backed by a read-only DB role.

## Key Findings

### Versions verified as of August 2026
- **Django 5.2 LTS** is the current LTS. Per Django's official 5.2 release notes: *"Django 5.2 is designated as a long-term support release. It will receive security updates for at least three years… Django 5.2 supports Python 3.10, 3.11, 3.12, 3.13, and 3.14 (as of 5.2.8)."* Support runs until **April 30, 2028**. Django 6.0 (released Dec 3, 2025, per the Django weblog post by Natalia Bidart) is a *standard* release supported only to **April 30, 2027**, and its notes state *"The Django 5.2.x series is the last to support Python 3.10 and 3.11."* The next LTS is **Django 6.2**, whose release notes confirm: *"Django 6.2 is designated as a long-term support release… Support for the previous LTS, Django 5.2, will end in April 2028. Django 6.2 supports Python 3.12, 3.13 and 3.14"* — expected **April 2027**. The user's doc mandates "a supported LTS," so **5.2 LTS is correct** — but note its remaining window is ~20 months, so plan an explicit LTS-to-LTS jump to 6.2 in 2027.
- **Python 3.13** is the sweet spot: supported by both 5.2 LTS today and 6.2 LTS later, and it avoids packaging-ecosystem lag on the newest 3.14.
- **PostgreSQL 18** is current stable. Per the PostgreSQL 18 press kit: *"September 25, 2025 - The PostgreSQL Global Development Group today announced the release of PostgreSQL 18."* The current minor is 18.4; PostgreSQL 19 is still in beta (Beta released June 2026) — do not use it. Django 5.2 supports PostgreSQL 14+.
- **Django's built-in `django.tasks` framework ships only in 6.0**, not 5.2, and even in 6.0 it provides only an API with no production worker. So this project **needs Celery** regardless.

---

## Details

### A. Windows development environment

**Recommendation: WSL2 (Ubuntu 24.04 LTS), not native Windows, not Dev Containers (yet).**

Concrete tradeoffs:
- **Celery**: Celery does not officially support Windows; the prefork worker pool is a Linux/Unix construct. [PyPI](https://pypi.org/project/celery/) On native Windows you fight `--pool=solo`/`eventlet` workarounds that don't match production. On WSL2 it just works.
- **File-watching / inotify**: Django's autoreloader and pytest watchers rely on inotify. Native Windows file events are slower and flaky; `/mnt/c` (Windows drive seen from WSL) has notoriously poor inotify and I/O performance. Keeping the repo on the **native WSL ext4 filesystem** (e.g. `~/projects/khanmandi`) is dramatically faster.
- **Path issues**: Mixed Windows/Linux path separators break scripts, `.env` files, and Docker mounts. Staying entirely inside WSL eliminates this class of bug.
- **Cloud parity**: Your cloud target is Linux. WSL2 Ubuntu gives you the same glibc, the same `apt` packages, the same Postgres locale behavior. This matters enormously for a financial system where Arabic collation and Decimal behavior must be identical dev→prod.
- **Playwright/Chromium**: `playwright install-deps` installs a long list of Chromium shared libraries via `apt`. This is trivial on Ubuntu and painful/unavailable natively. Same for headless font packages.
- **Dev Containers** are a good *option later* for onboarding a second developer, but add a layer of indirection now; start with plain WSL2 + Docker for data services.

**Exact installation steps (run in elevated PowerShell unless noted):**

```powershell
# 1. Install WSL2 + Ubuntu (reboot when prompted)
wsl --install
# 2. Verify you are on WSL 2
wsl --list --verbose        # VERSION column must read 2
# 3. Windows Terminal + gsudo (winget)
winget install Microsoft.WindowsTerminal
winget install gerardog.gsudo
```

Inside the Ubuntu shell:
```bash
# System update + build tooling
sudo apt update && sudo apt upgrade -y
sudo apt install -y build-essential curl git

# Git identity + line endings across the WSL/Windows boundary
git config --global user.name  "Your Name"
git config --global user.email "you@example.com"
git config --global init.defaultBranch main
git config --global core.autocrlf input     # store LF, checkout LF in WSL
git config --global core.eol lf
git config --global pull.rebase true

# SSH key for GitHub
ssh-keygen -t ed25519 -C "you@example.com"
eval "$(ssh-agent -s)" && ssh-add ~/.ssh/id_ed25519
cat ~/.ssh/id_ed25519.pub    # paste into GitHub → Settings → SSH keys
ssh -T git@github.com
```

Also add a repo-level `.gitattributes` with `* text=auto eol=lf` so line endings are enforced regardless of who clones. This prevents CRLF sneaking into migrations or fixtures.

**VS Code**: install VS Code on Windows, add the **WSL** extension (the current successor to "Remote-WSL"), then from WSL run `code .` inside the repo. All extensions, the Python interpreter, and the terminal then run *inside* Linux.

**Repo location**: `~/projects/khanmandi` on the WSL ext4 filesystem. **Never** put it under `/mnt/c/...` — I/O and inotify there are several times slower and will make the test suite and autoreload sluggish.

### B. Language, package, and dependency tooling

**Package manager: `uv` (Astral).** It replaces pip, pip-tools, virtualenv, and pyenv, [OneUptime](https://oneuptime.com/blog/post/2026-03-02-how-to-set-up-uv-fast-python-package-manager-on-ubuntu/view) is *"10-100x faster than pip and pip-tools"* (per uv's own docs), manages the Python interpreter itself, and produces a committed `uv.lock` for reproducible installs. It is now the mainstream 2026 default — Simon Willison noted uv was *"downloaded more than 126 million times last month"* in March 2026, at which point OpenAI announced it would acquire Astral and fold the team into its Codex effort; uv remains open-source (MIT).

```bash
# Install uv inside WSL
curl -LsSf https://astral.sh/uv/install.sh | sh
# New project + pinned Python
uv init khanmandi && cd khanmandi
uv python pin 3.13
```

**Recommended dependency set** (pin exact versions in `uv.lock`; verify latest patch at install time):

| Purpose | Package | Notes (Aug 2026) |
|---|---|---|
| Framework | `django==5.2.*` | Current LTS, EOL Apr 2028 |
| API | `django-ninja` (1.5.x) | 1.5.1+ needed for Django 6 later; 1.4.3 line for 5.2. Built on Pydantic v2 |
| Validation | `pydantic>=2` (2.13.x) | Ninja is built on it |
| DB driver | `psycopg[binary]==3.*` | `django.db.backends.postgresql` supports psycopg 3; enables `server_side_binding` |
| Config | `pydantic-settings` | Typed, fail-fast settings; you already have Pydantic |
| Task queue | `celery==5.6.3` + `django-celery-beat==2.8.1` | Redis broker; see gotcha below. Celery 5.6.3 released 2026-03-26 |
| Broker/cache | `redis` | |
| Tests | `pytest-django`, `factory_boy`, `hypothesis` | Property/stateful tests for costing |
| Lint+format | `ruff==0.15.*` | Replaces black + isort + flake8 + pyupgrade |
| Types | `mypy` + `django-stubs` | See type-checker note |
| Logging | `structlog` | Structured JSON logs |
| Monitoring | `sentry-sdk` | Error monitoring |
| Audit | `django-simple-history` | Complements the append-only ledgers |
| Static | `whitenoise` | |
| Server | `gunicorn` + `uvicorn` | Gunicorn with uvicorn workers for ASGI |
| PDF | `playwright` | + `playwright install chromium` |

Install with dependency groups:
```bash
uv add django "django-ninja" "psycopg[binary]" pydantic-settings \
        celery django-celery-beat redis structlog sentry-sdk \
        django-simple-history whitenoise gunicorn uvicorn playwright
uv add --dev ruff mypy django-stubs pytest-django factory_boy hypothesis pytest-cov
uv run playwright install chromium
uv run playwright install-deps         # apt libs for Chromium
```

**Type checker decision:** Use **mypy + django-stubs** as the CI gate. Astral's **ty** is extremely fast but still beta (0.0.61 as of July 2026, low typing-spec conformance, no plugin system and only partial Django support), so it is not yet safe as the authoritative gate for a Django ORM-heavy codebase. You may optionally add `ty` in-editor for near-instant feedback, but keep mypy+django-stubs as the merge gate until ty hits 1.0.

**Task-queue decision & gotcha (financial data):** Celery remains the mainstream production default and is the right choice here for its Canvas workflows (chains/chords/groups), Flower monitoring, and `django-celery-beat` scheduling — exactly what a system with scheduled closing jobs and imports needs. **Critical gotcha:** Celery-on-Redis acknowledges tasks *early* by default; if a worker crashes after pulling a job the task payload is lost from Redis. For a ledger system this is unacceptable, so set `task_acks_late = True` and `task_reject_on_worker_lost = True`, or — if transaction-loss tolerance is truly zero — use RabbitMQ for protocol-level acks. Use `django-celery-beat==2.8.1` for the business-date closing jobs (confirm the latest patch on PyPI at install time, as the readthedocs docs build lags).

**Notable deprecations/gotchas:** Django 5.2 dropped PostgreSQL 13; `psycopg2` support is on a deprecation path (use psycopg 3); pydantic v1 config (`Config` class) is deprecated in Ninja in favor of `Meta`; ruff 0.15 shipped a new "2026 style guide" so pin the ruff version to avoid formatting churn across machines.

### C. Docker and local infrastructure

**Recommendation: run Postgres + Redis in Docker Compose; run Django and the Celery worker *natively* in WSL.** Reasoning: the app and worker change constantly (fast reload, debugger attach, instant `pytest`), while the data services should be pinned, disposable, and identical to production. Running Django in Docker on Windows adds a bind-mount performance penalty and slows the reload loop.

**Docker Desktop**: install on Windows, enable the **WSL2 backend** and integration with your Ubuntu distro (Settings → Resources → WSL Integration).

`docker-compose.yml` (data services only):
```yaml
services:
  db:
    image: postgres:18
    environment:
      POSTGRES_DB: khanmandi
      POSTGRES_USER: khanmandi
      POSTGRES_PASSWORD: devpassword
      # Create the cluster with ICU + UTF-8 for correct Arabic sorting
      POSTGRES_INITDB_ARGS: "--encoding=UTF8 --locale-provider=icu --icu-locale=und"
    ports: ["5432:5432"]
    volumes: ["pgdata:/var/lib/postgresql/data"]
  redis:
    image: redis:7
    ports: ["6379:6379"]
volumes:
  pgdata:
```

**Database seeding/reset workflow (financial test data matters):**
- Keep a `make seed` target that loads deterministic reference data (org, branch Al-Bunook, warehouse/kitchen/cash point, chart of accounts, units of measure) via a management command, **not** random factories — reports must be reproducible.
- Keep `factory_boy` for transactional/property tests only.
- `make reset-db` should drop and recreate the Docker volume, run `migrate`, then `seed`. **Never** wire a destructive reset to anything Claude Code can trigger unprompted (see E).

### D. Repository structure and Phase 0 scaffolding

Recommended layout (modular monolith, service + selector + posting-policy layers):
```
khanmandi/
├── pyproject.toml
├── uv.lock
├── .env.example            # committed; real .env is gitignored
├── .gitignore  .editorconfig  .gitattributes
├── .pre-commit-config.yaml
├── docker-compose.yml  Dockerfile  Makefile
├── CLAUDE.md
├── .claude/
│   ├── settings.json        # committed team permissions
│   ├── settings.local.json  # gitignored personal overrides
│   ├── commands/            # custom slash commands
│   └── agents/              # subagents
├── config/                  # project (settings/urls/asgi/wsgi)
│   ├── settings/ (base.py, local.py, production.py)
│   ├── celery.py
├── apps/
│   ├── core/                # base models, money/Decimal, rounding policy, business-date
│   ├── organization/        # Org→Branch→Warehouse/Kitchen/Cash point
│   ├── inventory/           # StockMovement ledger (Phase 1)
│   │   ├── models.py  services.py  selectors.py  posting.py  tests/
│   ├── accounting/          # JournalEntry/JournalLine ledger
│   └── api/                 # Django Ninja routers/schemas
├── docs/
│   ├── specs/  adr/  requirements/traceability.md
│   ├── accounting/posting-rules/
│   └── testing/golden-cases/
└── tests/
```
Each app carries its own `services.py` (commands/transactions), `selectors.py` (reads/reports), and `posting.py` (posting-policy). This keeps the service/selector split visible and enforceable.

**Settings organization:** Use a `base/local/production` split under `config/settings/`, with all secrets and environment-specific values sourced through a `pydantic-settings` model (fail-fast, typed) rather than sprinkling `os.environ`. This gives startup-time validation — a `DEBUG="False"` string-truthiness bug can't reach production.

**`pyproject.toml`** essentials:
```toml
[project]
name = "khanmandi"
requires-python = ">=3.13"

[tool.ruff]
line-length = 100
target-version = "py313"
[tool.ruff.lint]
select = ["E","W","F","I","B","C4","UP","N","S","DJ"]  # incl. flake8-django, bandit
ignore = ["E501"]
[tool.ruff.format]
quote-style = "double"

[tool.pytest.ini_options]
DJANGO_SETTINGS_MODULE = "config.settings.local"
addopts = "--cov=apps --cov-report=term-missing --strict-markers"
python_files = "tests.py test_*.py *_tests.py"

[tool.mypy]
python_version = "3.13"
plugins = ["mypy_django_plugin.main"]
strict = true
[tool.django-stubs]
django_settings_module = "config.settings.local"

[tool.coverage.run]
branch = true
omit = ["*/migrations/*", "*/tests/*"]
```

**`.env` and secrets:** commit `.env.example`; gitignore real `.env`. Local dev uses the `.env` file; cloud uses the platform's secret manager / env vars. Never commit real secrets, and add a Claude deny rule on reading `.env*` (below).

**`.pre-commit-config.yaml`** (order matters — format, then lint-fix, then type, then fast checks):
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.15.14
    hooks:
      - id: ruff-format
      - id: ruff-check
        args: [--fix]
  - repo: local
    hooks:
      - id: mypy
        name: mypy
        entry: uv run mypy apps config
        language: system
        pass_filenames: false
      - id: migrations-check
        name: makemigrations --check
        entry: uv run python manage.py makemigrations --check --dry-run
        language: system
        pass_filenames: false
```

**Git conventions:** **trunk-based** development with short-lived branches and **Conventional Commits** (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`). This suits a solo/small-team financial project doing many small, reviewed commits, and makes the spec→test→implement→commit loop legible. Gate merges on the CI checks in section F.

### E. Claude Code setup

**Install: use the native installer, inside WSL2** (npm is now legacy; [AI Dev Tools](https://devtools.shingoirie.com/blog/en/claude-code-windows-setup-guide/) the native binary runs fine in WSL2, but **not** WSL1). [Morph](https://www.morphllm.com/install-claude-code) If you use npm instead, Node 22+ is required. [iTechs Online](https://itecsonline.com/post/how-to-install-claude-code-on-windows)
```bash
# inside WSL Ubuntu
curl -fsSL https://claude.ai/install.sh | sh   # installs to ~/.local/bin/claude
claude doctor                                  # confirms install type is native
```
A paid plan (Pro/Max/Team/Enterprise) is required to authenticate.

**CLAUDE.md** (loaded into context every session — keep it under ~200 lines; longer files dilute adherence). Reference docs with `@imports` and `file:line` pointers rather than pasting content. A good root file states: the stack and versions; the non-negotiable invariants (posted records immutable, corrections by reversal only, Decimal everywhere, moving weighted-average costing, business-date vs timestamp); the commands to run tests/lint/types; the layer rules (services vs selectors vs posting); and pointers to `docs/adr/`, `docs/accounting/posting-rules/`, and `docs/testing/golden-cases/`. Generate the skeleton with `/init`, then trim. Use `/memory` to view/edit loaded memory files.

**`.claude/settings.json` — permissions (this is the safety layer the doc demands).** Evaluation order is **deny → ask → allow, first match wins**, and a `deny` rule cannot be overridden by any allow rule or by a hook. [Claude API Docs](https://platform.claude.com/docs/en/agent-sdk/permissions) Put irreversible/destructive operations in `deny`, intent-changing operations in `ask`, and read/verify operations in `allow`:
```json
{
  "permissions": {
    "allow": [
      "Read", "Glob", "Grep",
      "Bash(git status)", "Bash(git diff:*)", "Bash(git log:*)",
      "Bash(uv run pytest:*)", "Bash(uv run ruff:*)", "Bash(uv run mypy:*)",
      "Bash(uv run python manage.py makemigrations:*)",
      "Bash(uv run python manage.py migrate:*)",
      "Edit(apps/**)", "Edit(config/**)", "Edit(tests/**)", "Edit(docs/**)"
    ],
    "ask": [
      "Bash(git add:*)", "Bash(git commit:*)", "Bash(git push:*)",
      "Bash(uv add:*)", "Bash(docker compose:*)"
    ],
    "deny": [
      "Bash(rm -rf:*)",
      "Bash(git push --force:*)", "Bash(git push -f:*)",
      "Bash(git reset --hard:*)",
      "Bash(*DROP DATABASE*)", "Bash(*DROP TABLE*)", "Bash(dropdb:*)",
      "Bash(*migrate --fake*)", "Bash(*migrate zero*)",
      "Bash(*flush*)",
      "Read(./.env)", "Read(./.env.*)", "Read(**/secrets/**)"
    ],
    "defaultMode": "acceptEdits"
  }
}
```
Because static globs can be evaded, also add a **PreToolUse hook** that inspects `Bash` commands and exits non-zero (blocking) if it sees destructive migration/database patterns — a belt-and-suspenders layer that fires even in permissive modes. (A PreToolUse hook exiting code 2 blocks the call even in bypass mode; hook decisions never override a `deny` rule.)

**Hooks:** a **PostToolUse** hook on `Write|Edit` that runs `ruff format` + `ruff check --fix` on the changed file (deterministic auto-format, no tokens spent), and optionally runs the affected test module. A **PreToolUse** hook as the destructive-command guard above.

**Other features to wire up:**
- **Plan mode** (Shift+Tab / `/permissions` → plan): a session structurally incapable of edits — use it for design and for reviewing before implementing.
- **Subagents** (`.claude/agents/`): e.g. a "posting-rules reviewer" and a "test-writer" agent.
- **Skills / custom slash commands** (`.claude/commands/`): e.g. `/new-spec`, `/golden-case`, `/adr`.
- **MCP Postgres server, read-only**: use a maintained server (e.g. `crystaldba/postgres-mcp` in `--access-mode=restricted`, or `mcp-server-pg --read-only`) — the original Anthropic reference Postgres server was **deprecated and archived in 2025**, so avoid it. Critically, back the MCP with a **dedicated read-only DB role**, not the app user:
  ```sql
  CREATE USER claude_ro WITH PASSWORD '...';
  GRANT CONNECT ON DATABASE khanmandi TO claude_ro;
  GRANT USAGE ON SCHEMA public TO claude_ro;
  GRANT SELECT ON ALL TABLES IN SCHEMA public TO claude_ro;
  ```
  Database-level read-only is the only real guarantee; the MCP flag is secondary. Configure it in a project-scoped `.mcp.json` and pass the connection string via an environment variable, not inline.
- **Checkpointing/rewind** and **background tasks**: use rewind to undo an agent run gone wrong; use background tasks for long-running jobs.

**Workflow (spec → test → implement → review → commit):**
1. **Spec first** — in plan mode, have Claude draft/update `docs/specs/…` and an ADR; you review before any code.
2. **Test first** — write the failing pytest/Hypothesis tests and a golden case in `docs/testing/golden-cases/`.
3. **Implement** — accept-edits mode within the app; hooks auto-format.
4. **Review diff** — `git diff` (allowed) and read the change before staging.
5. **Commit** — `git add`/`git commit` are in `ask`, so you approve each commit; Conventional Commit message.

**Context management for a long multi-phase project:** use `/clear` between unrelated tasks; `/compact` when a session grows long; reference docs via `@docs/...` imports so the model re-reads authoritative specs rather than relying on drifting memory; end each session by having Claude update the relevant spec/ADR so the next session resumes from written state, not chat history.

**First-session sequence on the fresh repo** — see the Day-1 checklist below (steps 21–24).

### F. Cloud deployment preparation (early decisions only)

**Region/provider from Baghdad:** The lowest-latency major-cloud regions are **AWS Bahrain (`me-south-1`) and AWS UAE (`me-central-1`)**, and **Azure UAE North / Qatar Central**. For this workload (single restaurant group, low traffic, financial data), the deciding factor is **managed Postgres with point-in-time recovery**, not raw compute.

**Recommendation:**
- **Simplest path with real PITR:** **Render** (managed Postgres with daily backups + PITR, background-worker + cron-job primitives, flat pricing) — it is the closest Heroku replacement now that Heroku moved to sustaining-engineering mode in Feb 2026. Deploy the web service (gunicorn+uvicorn), a Celery worker, and a Celery beat cron.
- **If you want the DB physically in-region near Baghdad:** run app compute on a Middle-East cloud/VPS (AWS me-central/me-south, or a regional VPS) with **AWS RDS for PostgreSQL** (native automated backups + PITR) as the database. Do **not** use Railway's containerized Postgres or Fly.io *unmanaged* Postgres for financial data — neither gives you PITR out of the box (Fly's Managed Postgres does, at a higher price).
- **Payment note:** hyperscalers bill in USD via card; confirm the client can settle USD card payments from Iraq, otherwise a regional provider (e.g. a Gulf VPS accepting alternative payment) may be more practical.

**Decisions that are expensive to change later — make them now:**
- **Backups/PITR**: choose a Postgres offering with automated backups **and** point-in-time recovery on day one. For a ledger-based financial system this is non-negotiable.
- **Timezone**: `TIME_ZONE = "Asia/Baghdad"`, `USE_TZ = True`. Store timestamps in UTC, but the **restaurant business date** is a separate `DateField` derived per your policy (operations run past midnight) — never `date(timestamp)`.
- **Encoding & collation**: create the database with **UTF-8** encoding and an **ICU** locale so Arabic text sorts correctly (`--locale-provider=icu`). From PostgreSQL 15+ you can set an ICU collation as the database default; changing encoding/collation later means a full dump/reload, so set it at `initdb`/`createdb` time in both dev and prod. Keep dev (Docker) and prod identical.
- **Migration strategy for prod**: run `migrate` as a release/pre-deploy step; follow expand-contract (add nullable column → backfill → add constraint) and never combine a destructive schema change with a code change in one deploy.
- **Environment parity**: same Postgres major (18), same locale, same Python (3.13) dev→CI→prod.

**CI/CD (GitHub Actions)** — gate merges on all of these:
```yaml
name: ci
on: [pull_request, push]
jobs:
  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:18
        env: { POSTGRES_PASSWORD: postgres, POSTGRES_DB: test }
        ports: ["5432:5432"]
        options: >-
          --health-cmd pg_isready --health-interval 10s
          --health-timeout 5s --health-retries 5
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { enable-cache: true }
      - run: uv sync --frozen
      - run: uv run ruff format --check .
      - run: uv run ruff check .
      - run: uv run mypy apps config
      - run: uv run python manage.py makemigrations --check --dry-run
      - run: uv run pytest --cov=apps
```
Gate on: ruff format clean, ruff lint clean, mypy clean, **no missing migrations**, and tests passing with a coverage floor.

### G. Arabic / RTL / PDF validation spike

**Recommendation: Playwright/Chromium HTML→PDF.** Chromium renders Arabic shaping and bidirectional text correctly through its own HarfBuzz-based engine — the same rendering you see in Chrome. Alternatives are weaker:
- **WeasyPrint** uses Pango + HarfBuzz for shaping, [DeepWiki](https://deepwiki.com/Kozea/WeasyPrint/4.3-font-system-and-text-layout) but its own official docs' CSS 2.1 "not supported" list explicitly includes *"Right-to-left or bi-directional text,"* and there are open issues where justified + bold Arabic overlaps. [GitHub](https://github.com/Kozea/WeasyPrint/issues/1640) Riskier for invoices/reports.
- **ReportLab** only added RTL/shaping in v4.4.0 (released 17 April 2025), described in its own release notes as *"Experimental support for Right to Left text (RTL)… Experimental support for character shaping using Harfbuzz, used for many south asian languages and Arabic,"* and it needs a separate bidi package. Still experimental.
- **LibreOffice headless** works but is a heavy dependency and templating is clunky.

Use `page.pdf()` (Chromium-only) [Docupotion](https://docupotion.com/blog/generate-pdfs-playwright) with `printBackground: true` and `waitUntil: "networkidle"`. Render a Django template → HTML → Chromium → PDF.

**Fonts** — all four candidates are **SIL Open Font License 1.1**, free for commercial embedding in PDFs (verified against each project's repo/font metadata):
- **Noto Naskh Arabic** (OFL-1.1, The Noto Project Authors), **Amiri** (OFL-1.1, Amiri Project Authors), **IBM Plex Sans Arabic** (OFL-1.1, IBM Corp.), **Cairo** (OFL-1.1, The Cairo Project Authors). OFL requires shipping the license text *with the font files* when you distribute the fonts, [Wikipedia](https://en.wikipedia.org/wiki/SIL_Open_Font_License) but not with the PDFs they render.
- Recommendation: **Noto Naskh Arabic** for body/report text (broad coverage, neutral), **Amiri** if you want a traditional Naskh look for headings.
- Install in the Docker/Linux container:
  ```dockerfile
  RUN apt-get update && apt-get install -y --no-install-recommends \
      fonts-noto-core fonts-hosny-amiri fontconfig \
      && rm -rf /var/lib/apt/lists/*
  ```
  `fonts-noto-core` contains Noto Naskh/Sans/Kufi Arabic; [Debian](https://packages.debian.org/trixie/fonts-noto-core) the old `fonts-noto` metapackage is obsolete (Debian describes it as an *"obsolete metapackage"*). Vendor Cairo and IBM Plex Sans Arabic directly from their OFL repos, as their apt packaging is inconsistent across releases.

**Arabic PDF test-document checklist (run this prototype in Foundations):**
- Mixed Arabic + Latin in one line (branch name + Latin SKU).
- Arabic text with embedded Western digits and IQD amounts (`1,250,000 د.ع`) — verify digits stay LTR inside RTL text.
- A right-to-left table: headers on the right, numeric columns aligned correctly, totals row.
- Currency with Decimal rounding per your written policy (no float artifacts).
- Long Arabic paragraph wrapping + justification (this is where WeasyPrint fails).
- Page break across a multi-page invoice with a repeating RTL header/footer.
- Diacritics/ligatures (لا, hamza forms) render as connected glyphs, not isolated forms.
- The business date and timestamp both rendered, in the correct calendar/format.

**Django i18n + RTL:**
- `USE_I18N = True`, `LANGUAGES = [("ar","Arabic"),("en","English")]`, `LocaleMiddleware`, `locale/` dirs, `makemessages -l ar` / `compilemessages`.
- In templates set `<html dir="{% if LANGUAGE_BIDI %}rtl{% else %}ltr{% endif %}" lang="{{ LANGUAGE_CODE }}">`.
- Use **CSS logical properties** (`margin-inline-start`, `padding-inline-end`, `text-align: start`) instead of left/right so one stylesheet serves both directions.
- Test RTL early by switching the active language and rendering both the web view and a PDF in the spike.

**Frontend choice (still open — frame as a decision):** Django templates + HTMX vs React. For a bilingual RTL ERP with server-rendered PDF reports and a small team, **Django templates + HTMX is the lower-risk default** — it keeps rendering, i18n, and RTL in one place, reuses the same templates for PDF generation, and avoids a second build toolchain; choose React only if you need a highly interactive client (offline POS, complex real-time UI). Decide this before Phase 1 UI work; it does not block Phase 0.

---

## H. Day 1 → Day N ordered checklist

*(⏱ = rough time; 🔂 = recurring, 1️⃣ = one-time)*

**Day 1 — Machine & OS foundation (~2–3 h, all 1️⃣)**
1. Install WSL2 + Ubuntu 24.04; verify `wsl -l -v` shows VERSION 2; reboot. ⏱30m
2. Install Windows Terminal + gsudo via winget. ⏱10m
3. `apt update && upgrade`; install `build-essential curl git`. ⏱15m
4. Configure Git identity + `core.autocrlf input` + `core.eol lf`. ⏱10m
5. Generate ed25519 SSH key, add to GitHub, `ssh -T git@github.com`. ⏱15m
6. Install VS Code (Windows) + WSL extension; open a WSL folder with `code .`. ⏱20m
7. Install Docker Desktop (Windows), enable WSL2 backend + Ubuntu integration. ⏱30m

**Day 1–2 — Language & project scaffold (~2 h, 1️⃣)**
8. Install `uv`; `uv init khanmandi`; `uv python pin 3.13`; put repo at `~/projects/khanmandi` (never `/mnt/c`). ⏱20m
9. `uv add` the runtime + dev dependency sets (section B). ⏱20m
10. `uv run playwright install chromium && uv run playwright install-deps`. ⏱15m
11. Create the directory layout + `config/settings/{base,local,production}.py` with pydantic-settings. ⏱45m
12. Write `pyproject.toml` (ruff/pytest/mypy/coverage), `.gitattributes`, `.editorconfig`, `.gitignore`, `.env.example`. ⏱30m

**Day 2 — Local infrastructure (~1.5 h, 1️⃣ + 🔂 usage)**
13. Write `docker-compose.yml` (Postgres 18 with ICU/UTF-8, Redis); `docker compose up -d`. ⏱20m
14. `django-admin startproject`; wire settings to Postgres via env; `manage.py migrate`. ⏱30m
15. Write `Makefile` targets: `seed`, `reset-db`, `test`, `lint`, `types`. ⏱30m
16. Create the read-only `claude_ro` DB role. ⏱10m

**Day 2–3 — Quality gates & Claude Code (~2.5 h, 1️⃣)**
17. Install & configure pre-commit (ruff → mypy → migrations-check); `pre-commit install`. ⏱30m
18. Add `.github/workflows/ci.yml`; push and confirm the pipeline is green. ⏱40m
19. Install Claude Code (native, in WSL); `claude doctor`; authenticate. ⏱20m
20. Write `.claude/settings.json` (deny/ask/allow), the PreToolUse destructive-command hook, the PostToolUse ruff hook, and the read-only Postgres MCP config. ⏱45m
21. Run `/init` to seed `CLAUDE.md`; trim to <200 lines with the invariants + doc pointers. ⏱30m

**Day 3–4 — Arabic PDF & i18n spike (~half day, 1️⃣, must pass before coding)**
22. Add fonts to the Dockerfile (`fonts-noto-core`, `fonts-hosny-amiri`, `fontconfig`). ⏱15m
23. Build a Django template → Chromium `page.pdf()` proof: render the checklist invoice in Arabic. ⏱2–3h
24. Configure i18n (ar/en, LocaleMiddleware, logical-property CSS); switch language and re-render web + PDF. ⏱1h
25. Sign-off gate: the Arabic invoice renders correctly (shaping, bidi, RTL table, IQD digits). If not, evaluate LibreOffice headless — *not* WeasyPrint. 1️⃣

**Day 4 — First commit & first Claude Code session (~1 h, transition to Phase 0)**
26. Initial commit of the scaffold (Conventional Commit: `chore: bootstrap project scaffold`); push. 🔂
27. First Claude Code session in **plan mode**: point it at `docs/specs/` + `CLAUDE.md`, ask it to draft the Phase 0 Foundations spec/ADR (money/Decimal type, rounding policy, business-date helper, org hierarchy). Review, then proceed test-first. **← This is the stopping point; feature coding begins here.**

---

## Recommendations

**Staged plan:**
1. **Now (Phase 0 setup):** WSL2 + uv + Django 5.2 LTS + Python 3.13 + Postgres 18 in Docker with ICU/UTF-8; commit the scaffold; wire Claude Code permissions/hooks; run the Arabic PDF spike. **Do not start Phase 0 feature coding until the spike renders a correct Arabic invoice.**
2. **Before Phase 1:** finalize the frontend decision (HTMX vs React) and stand up the cloud DB with PITR + `Asia/Baghdad` + ICU collation so dev/prod are identical.
3. **2027:** plan the LTS-to-LTS upgrade from Django 5.2 → 6.2 once 6.2 ships (April 2027) and django-ninja/celery confirm 6.2 support; that jump also unlocks the built-in `django.tasks` API if you later want to reduce Celery's footprint.

**Thresholds that change the recommendation:**
- If you must keep the database physically in Iraq for data-residency reasons → drop Render, use a Gulf/Iraq VPS + self-managed Postgres with a rigorously tested PITR setup (e.g. pgBackRest).
- If transaction-loss tolerance is truly zero → switch the Celery broker from Redis to RabbitMQ.
- If `ty` reaches a stable 1.0 with django-stubs-equivalent support → promote ty to the CI gate and retire mypy.
- If the PDF spike shows Chromium struggling with a specific report → the fallback is LibreOffice headless, not WeasyPrint.

## Caveats
- **Verify exact patch versions at install time.** Version numbers move; confirm current `django-ninja`, `ruff`, `celery`, `django-celery-beat`, and `uv` on PyPI when you install. Several package versions here (e.g. django-ninja 1.5.x, django-celery-beat 2.8.1) come from release notes/aggregators rather than a single canonical page.
- **Django 5.2 LTS has ~20 months of support left** (to April 30, 2028). This is the correct LTS today, but budget the 6.2 upgrade explicitly rather than treating 5.2 as multi-year-stable.
- **Some Claude Code specifics evolve fast** (model names, auto-mode, memory subsystems). Treat the permissions/hook schema here as current-as-of-mid-2026 and re-check the official Claude Code docs at setup.
- **The read-only DB role is the real guardrail**, not the MCP's read-only flag — configure both.
- Cloud pricing/region availability and payment-from-Iraq practicalities should be confirmed directly with the provider before committing.
