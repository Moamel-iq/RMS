# ADR-010 — Windows-native development environment and pip-tools

- **Status:** Accepted
- **Date:** 2026-08-08
- **Related:** ADR-001, `docs/plans/installation-to-coding-start-plan.txt`,
  `docs/plans/environment-setup-wsl-alternative.md`

## Context

Two planning documents prescribed incompatible environments:

| | Installation plan | WSL alternative |
|---|---|---|
| OS | Windows 11 native | WSL2 Ubuntu |
| Python | 3.14 | 3.13 |
| Packaging | venv + pip-tools | uv |
| Docker / Celery / Redis | deferred | day one |
| IDE | PyCharm | VS Code |

Both are internally coherent. Running them simultaneously is not possible, and
leaving the conflict unresolved would have meant every future session picking
whichever document it read last.

## Decision

Follow the **Installation-to-Coding-Start plan**: Windows 11 native, Python
3.14, `venv` + `pip-tools`, PyCharm, PostgreSQL 18 installed locally. Docker,
Celery, Redis, Playwright, and openpyxl are added only when the feature that
needs them arrives.

`docs/plans/environment-setup-wsl-alternative.md` and
`docs/plans/claude-md-wsl-variant-superseded.md` are retained as reference, not
as instructions. The superseded `CLAUDE.md` variant describes the uv/WSL stack
and must not be followed.

### pip is pinned to 26.1.2

pip-tools 7.6.0 — the current release — declares only `pip>=22.2` but calls
pip internals that changed in pip 26.2. With pip 26.2.1 installed,
`pip-compile` fails:

```
TypeError: RequirementCommand.make_requirement_preparer() missing 1 required
keyword-only argument: 'allow_editables'
```

The project venv therefore holds pip **26.1.2**. pip's own "new release
available" notice must be ignored until pip-tools ships a compatible version.

## Alternatives considered

- **WSL2 + uv** — better cloud parity, working Celery prefork, faster
  file-watching, and simpler Playwright/Chromium setup. Genuinely stronger for
  the later phases. Rejected now because the developer's chosen plan, IDE, and
  installed toolchain are Windows-native, and switching mid-Phase-0 costs more
  than it currently returns.
- **Upgrading pip and switching to `uv pip compile`** — would resolve the pip
  conflict, but introduces the packaging tool this ADR just declined.

## Consequences

- **Celery will not run well on Windows.** Its prefork pool is Unix-only.
  When the first background job arrives (Phase 7 closing jobs, imports), this
  decision must be revisited — either WSL2, Docker, or a Windows-compatible
  pool. This is a known deferred cost, not an oversight.
- Arabic PDF rendering via Playwright/Chromium is unproven on this setup. The
  spike must run before any report work begins.
- Production is Linux. Dev/prod parity is weaker than the WSL option would have
  given, so CI on `ubuntu-latest` is the parity gate and must stay green.
- Dependency changes require `pip-compile` then `pip-sync`; the generated
  `.txt` locks are never hand-edited.
