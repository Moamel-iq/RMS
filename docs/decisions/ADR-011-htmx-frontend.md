# ADR-011 — Django templates + htmx for the frontend

- **Status:** Accepted
- **Date:** 2026-08-08
- **Related:** ADR-010, `docs/architecture/architecture-charter.md` (Part 1B, Foundations checklist)

## Context

The architecture charter left the frontend open: "Choose separately between
Django templates + HTMX or a React frontend based on the actual UI and team
capacity." The decision could not be deferred past the first screen.

The system is a bilingual Arabic/English RTL ERP whose reports are rendered
server-side to PDF. Its UI is forms, tables, and documents — not a real-time
or offline-capable client.

## Decision

Django templates with **htmx 2.0.4**, vendored at
`static/vendor/htmx.min.js`. No CDN, no Node toolchain, no bundler.

Supporting choices made with it:

- **Arabic is the source language.** `LANGUAGE_CODE = "ar"`, and message IDs
  are the Arabic strings themselves. English becomes a translation target.
  This inverts the usual English-msgid convention; it was chosen because the
  operators are Arabic-speaking and because `gettext`/`msgfmt` is not
  installed on the development machine, so `compilemessages` cannot run and an
  uncompiled catalog would render English msgids to Arabic users.
- **CSS logical properties only** (`padding-inline-start`, `text-align: start`,
  `inset-inline-start`) so one stylesheet serves both directions.
- **Browser language negotiation is disabled.** See
  `config.middleware.ExplicitLocaleMiddleware`.

## Alternatives considered

- **React** — warranted only by a highly interactive client such as an offline
  POS. It would add a second build toolchain, a second i18n/RTL implementation,
  and would not share templates with the PDF renderer.
- **CDN-hosted htmx** — rejected. A login page must not issue a third-party
  request, and an offline or restricted network must not break sign-in.
- **Django's LocaleMiddleware** — rejected; see below.

## Consequences

- Templates are shared between the web UI and the future Chromium PDF
  renderer, so RTL behaviour is proven once.
- No `npm`, no bundler, no build step. Static files ship as-is.
- Upgrading htmx means replacing a vendored file deliberately, not a silent
  transitive bump. Its version is recorded here and in the file itself.
- **`compilemessages` cannot run until gettext is installed.** Until then the
  English catalog cannot be produced and the UI is Arabic-only. This must be
  resolved before English is offered to users.
- Every htmx endpoint must decide what it returns to a fragment request versus
  a full page load. The pattern established in `apps/users/views.py` is:
  re-render the fragment with HTTP 200 on validation failure (htmx does not
  swap error responses by default), and return `HX-Redirect` on success so the
  browser navigates rather than swapping a whole page into a form element.

### Why not Django's LocaleMiddleware

It falls back to the browser's `Accept-Language` header. A manager whose
Windows is set to English would be served Arabic text inside a left-to-right
layout — every field icon and label on the wrong side. This was observed in
the browser during Task 0.2, not predicted. `ExplicitLocaleMiddleware` takes
the language from the language cookie or the site default, and ignores the
browser entirely.
