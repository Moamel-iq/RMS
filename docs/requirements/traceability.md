# Requirements traceability

Maps each requirement to the code and tests that satisfy it. Updated as part of
every task's definition of done.

| Req ID | Summary | Module | Model / service / API | Tests | Status | Notes |
|---|---|---|---|---|---|---|
| ENV-001 | PostgreSQL is the only database; SQLite never used | config | `config/settings/base.py` DATABASES | `tests/test_settings.py::TestDatabaseConfiguration` | Done | ADR-002 |
| ENV-002 | Timezone is Asia/Baghdad with USE_TZ enabled | config | `config/settings/base.py` | `tests/test_settings.py::TestTimeConfiguration` | Done | Business date is separate; see ADR-008 (pending) |
| ENV-003 | Arabic and English are configured with correct middleware order | config | `config/settings/base.py` | `tests/test_settings.py::TestInternationalization` | Done | RTL rendering not yet proven |
| ENV-004 | Secrets come from the environment and fail fast when absent | config | `config/settings/base.py`, `production.py` | `tests/test_settings.py::TestSecretHandling`, `::TestProductionGuards` | Done | |
| ENV-005 | Production refuses to boot in an unsafe configuration | config | `config/settings/production.py` | `tests/test_settings.py::TestProductionGuards` | Done | DEBUG, wildcard hosts, dev secret |
| API-001 | Versioned API exists at /api/v1/ | config | `config/api.py`, `config/urls.py` | `tests/test_health.py` | Done | Django Ninja |
| API-002 | Health endpoint reports app and database readiness | config | `config/api.py::health` | `tests/test_health.py::TestHealthEndpoint` | Done | 200 healthy / 503 database down |
| API-003 | Health endpoint leaks no configuration detail | config | `config/api.py::health` | `tests/test_health.py::test_body_leaks_no_configuration_detail` | Done | |
| USR-001 | Custom user model exists before the first migration | users | `apps/users/models.py::User` | `apps/users/tests/test_models.py::TestUserModelIsWiredUp` | Done | AUTH_USER_MODEL = users.User |
| USR-002 | A phone number identifies exactly one account | users | `apps/users/phone.py`, `User.phone` unique | `apps/users/tests/test_phone.py`, `::TestPhoneUniqueness` | Done | Canonical form +9647XXXXXXXXX |
| USR-003 | Malformed phone numbers are rejected by the database, not only Python | users | `User.Meta.constraints` | `apps/users/tests/test_models.py::TestDatabaseConstraints` | Done | Two CHECK constraints |
| USR-004 | Users sign in with either a username or a phone number | users | `apps/users/backends.py::PhoneOrUsernameBackend` | `apps/users/tests/test_backends.py` | Done | Derived from the login screen design; no SRS |
| USR-005 | Sign-in failures do not reveal whether an account exists | users | `LoginForm.error_messages`, backend | `::TestNoUserEnumeration`, `::TestNoUserEnumerationFromTheWeb` | Done | Same message and same hashing cost either way |
| USR-006 | An ambiguous identifier fails closed | users | `PhoneOrUsernameBackend` | `apps/users/tests/test_backends.py::TestAmbiguousIdentifier` | Done | MultipleObjectsReturned → deny |
| USR-007 | Inactive accounts cannot sign in | users | ModelBackend `user_can_authenticate` | `::test_inactive_user_cannot_authenticate` | Done | |
| USR-008 | Logout requires POST | users | `apps/users/views.py::LogoutView` | `apps/users/tests/test_login_views.py::TestLogout` | Done | GET returns 405 |
| UI-001 | Interface is Arabic RTL by default | config, templates | `ExplicitLocaleMiddleware`, `templates/base.html` | `::test_page_renders_rtl_when_arabic_is_selected` | Done | ADR-011 |
| UI-002 | Browser Accept-Language cannot flip layout direction | config | `config/middleware.py` | `::test_browser_language_cannot_flip_the_layout` | Done | Bug found in-browser during Task 0.2 |
| UI-003 | One stylesheet serves both directions | static | `static/css/app.css` | `::test_page_renders_ltr_when_english_is_selected` | Done | CSS logical properties |
| UI-004 | No third-party requests on the login page | templates | `static/vendor/htmx.min.js` | `::test_htmx_is_served_locally_not_from_a_cdn` | Done | htmx 2.0.4 vendored |
| UI-005 | Failed sign-in re-renders inline without losing input | users | `LoginView.form_invalid` | `apps/users/tests/test_login_views.py::TestHtmxLogin` | Done | htmx fragment swap |

## Not yet mapped

The SRS has not been added to this repository. `docs/requirements/SRS.md` is
referenced by `CLAUDE.md` but does not exist. Until it is supplied, requirement
IDs above are local to the bootstrap and are not traceable to a business
source.
