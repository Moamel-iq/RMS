# Requirements traceability

Maps each requirement to the code and tests that satisfy it. Updated as part of
every task's definition of done.

| Req ID | Summary | Module | Model / service / API | Tests | Status | Notes |
|---|---|---|---|---|---|---|
| ENV-001 | PostgreSQL is the only database; SQLite never used | config | `config/settings/base.py` DATABASES | `tests/test_settings.py::TestDatabaseConfiguration` | Done | ADR-002 |
| ENV-002 | Timezone is Asia/Baghdad with USE_TZ enabled | config | `config/settings/base.py` | `tests/test_settings.py::TestTimeConfiguration` | Done | Business date is separate; see ADR-008 (pending) |
| ENV-003 | Arabic and English are configured with correct middleware order | config | `config/settings/base.py` | `tests/test_settings.py::TestInternationalization` | Done | RTL rendering not yet proven |
| ENV-004 | Secrets come from the environment and fail fast when absent | config | `config/settings/base.py`, `production.py` | `tests/test_settings.py::TestSecretHandling`, `test_settings.py::TestProductionGuards` | Done | |
| ENV-005 | Production refuses to boot in an unsafe configuration | config | `config/settings/production.py` | `tests/test_settings.py::TestProductionGuards` | Done | DEBUG, wildcard hosts, dev secret |
| API-001 | Versioned API exists at /api/v1/ | config | `config/api.py`, `config/urls.py` | `tests/test_health.py` | Done | Django Ninja |
| API-002 | Health endpoint reports app and database readiness | config | `config/api.py::health` | `tests/test_health.py::TestHealthEndpoint` | Done | 200 healthy / 503 database down |
| API-003 | Health endpoint leaks no configuration detail | config | `config/api.py::health` | `tests/test_health.py::test_body_leaks_no_configuration_detail` | Done | |
| USR-001 | Custom user model exists before the first migration | users | `apps/users/models.py::User` | `apps/users/tests/test_models.py::TestUserModelIsWiredUp` | Done | AUTH_USER_MODEL = users.User |
| USR-002 | A phone number identifies exactly one account | users | `apps/users/phone.py`, `User.phone` unique | `apps/users/tests/test_phone.py`, `apps/users/tests/test_models.py::TestPhoneUniqueness` | Done | Canonical form +9647XXXXXXXXX |
| USR-003 | Malformed phone numbers are rejected by the database, not only Python | users | `User.Meta.constraints` | `apps/users/tests/test_models.py::TestDatabaseConstraints` | Done | Two CHECK constraints |
| USR-004 | Users sign in with either a username or a phone number | users | `apps/users/backends.py::PhoneOrUsernameBackend` | `apps/users/tests/test_backends.py` | Done | Derived from the login screen design; no SRS |
| USR-005 | Sign-in failures do not reveal whether an account exists | users | `LoginForm.error_messages`, backend | `test_backends.py::TestNoUserEnumeration`, `test_login_views.py::TestNoUserEnumerationFromTheWeb` | Done | Same message and same hashing cost either way |
| USR-006 | An ambiguous identifier fails closed | users | `PhoneOrUsernameBackend` | `apps/users/tests/test_backends.py::TestAmbiguousIdentifier` | Done | MultipleObjectsReturned → deny |
| USR-007 | Inactive accounts cannot sign in | users | ModelBackend `user_can_authenticate` | `test_backends.py::test_inactive_user_cannot_authenticate` | Done | |
| USR-008 | Logout requires POST | users | `apps/users/views.py::LogoutView` | `apps/users/tests/test_login_views.py::TestLogout` | Done | GET returns 405 |
| UI-001 | Interface is Arabic RTL by default | config, templates | `ExplicitLocaleMiddleware`, `templates/base.html` | `test_login_views.py::test_page_renders_rtl_when_arabic_is_selected` | Done | ADR-011 |
| UI-002 | Browser Accept-Language cannot flip layout direction | config | `config/middleware.py` | `test_login_views.py::test_browser_language_cannot_flip_the_layout` | Done | Bug found in-browser during Task 0.2 |
| UI-003 | One stylesheet serves both directions | static | `static/css/app.css` | `test_login_views.py::test_page_renders_ltr_when_english_is_selected` | Done | CSS logical properties |
| UI-004 | No third-party requests on the login page | templates | `static/vendor/htmx.min.js` | `test_login_views.py::test_htmx_is_served_locally_not_from_a_cdn` | Done | htmx 2.0.4 vendored |
| UI-005 | Failed sign-in re-renders inline without losing input | users | `LoginView.form_invalid` | `apps/users/tests/test_login_views.py::TestHtmxLogin` | Done | htmx fragment swap |
| ORG-001 | Branch belongs to exactly one organization | organizations | `Branch.organization` (PROTECT) | `apps/organizations/tests/test_models.py::TestBranch` | Done | ADR-007 |
| ORG-002 | Branch codes are unique within an organization, not globally | organizations | `branch_code_unique_per_organization` | `apps/organizations/tests/test_models.py::test_two_organizations_may_reuse_the_same_branch_code` | Done | |
| ORG-003 | Bilingual names on organization and branch | organizations | `name_ar`, `name_en` | `apps/organizations/tests/test_models.py::test_names_are_stored_in_both_languages` | Done | Stored data, not translated strings |
| ORG-004 | Branch carries its own timezone and operating-day cutoff | organizations | `Branch.timezone`, `business_day_start_time` | `apps/organizations/tests/test_models.py::test_unknown_timezones_are_rejected`, `apps/organizations/tests/test_models.py::test_business_day_start_time_is_required` | Done | ADR-008; cutoff **value** still open |
| ORG-005 | A user may hold access to several branches | organizations | `BranchMembership` | `apps/organizations/tests/test_models.py::test_a_user_may_hold_several_branches` | Done | Why User has no branch field |
| ORG-006 | One role per user per branch | organizations | `membership_unique_per_user_and_branch` | `apps/organizations/tests/test_models.py::test_one_role_per_user_per_branch` | Done | |
| ORG-007 | A user cannot access an unassigned branch | organizations | `selectors.can_access_branch` | `apps/organizations/tests/test_selectors.py::TestAccessIsGranted` | Done | |
| ORG-008 | Cross-organization access is rejected | organizations | `selectors.accessible_branches` | `test_selectors.py::test_member_does_not_see_another_organizations_branch` | Done | The isolation test |
| ORG-009 | Inactive user, branch, or organization removes access | organizations | `selectors.accessible_branches` | `test_selectors.py::TestAccessIsWithdrawn` | Done | |
| ORG-010 | Revoking access preserves the record | organizations | `services.revoke_branch_access` | `test_selectors.py::test_revoking_keeps_the_record` | Done | Deactivate, never delete |
| ORG-011 | Organizations and branches cannot be deleted while referenced | organizations | `on_delete=PROTECT` | `apps/organizations/tests/test_models.py::test_organization_cannot_be_deleted_while_branches_exist` | Done | |
| ORG-012 | Superuser access is explicit and holds no implied role | organizations | `selectors.accessible_branches`, `role_at_branch` | `test_selectors.py::TestSuperuser` | Done | |
| NAV-001 | Shell shows every module in the approved build order | core | `apps/core/navigation.py` | `apps/core/tests/test_shell.py::TestNavigationDefinition` | Done | |
| NAV-002 | Unbuilt sections are visible but inert | core | `templates/shell.html` | `test_shell.py::test_unbuilt_sections_are_inert` | Done | No links to 404s |
| NAV-003 | Any module's sidebar can be previewed before it is built | core | `context_processors.shell` | `test_shell.py::test_every_module_sidebar_can_be_previewed` | Done | `?module=` |
| NAV-004 | Unknown module parameter falls back safely | core | `context_processors.shell` | `test_shell.py::test_unknown_module_falls_back_instead_of_erroring` | Done | Allow-list of known keys |
| QTY-001 | Quantities store at 3 dp, ROUND_HALF_UP | core | `apps/core/quantity.py::quantize_quantity` | `apps/core/tests/test_quantity.py::TestRoundingDirection` | Done | ADR-006 |
| QTY-002 | Ties round away from zero, symmetrically | core | `QUANTITY_ROUNDING` | `test_quantity.py::test_negative_ties_round_away_from_zero`, `test_quantity.py::test_rounding_is_symmetric_in_magnitude` | Done | Required for reversals to cancel exactly |
| QTY-003 | Rounding happens once, never mid-calculation | core, units | `convert` vs `convert_to_stored_quantity` | `test_quantity.py::TestNoDoubleRounding`, `test_conversion.py::test_convert_does_not_round` | Done | 1.00049999 distinguishes the paths |
| QTY-004 | No float may enter a quantity path | core | `ensure_decimal` | `test_quantity.py::TestFloatRejection` | Done | bool and non-finite rejected too |
| QTY-005 | Arabic-Indic numerals accepted; mixed scripts refused | core | `normalize_digits` | `test_quantity.py::TestArabicNumerals` | Done | Matters for Phase 8 OCR ingestion |
| QTY-006 | Money cannot reuse quantity rounding | core | `apps/core/quantity.py` naming | — | Partial | Enforced by separation and naming; money module not yet written |
| UOM-001 | Every dimension has exactly one base unit | units | `unit_one_base_per_dimension` | `apps/units/tests/test_models.py::TestSeed`, `apps/units/tests/test_models.py::TestDatabaseConstraints` | Done | Partial unique index |
| UOM-002 | Conversion factors are positive and base factor is 1 | units | `unit_factor_is_positive`, `unit_base_factor_is_one` | `apps/units/tests/test_models.py::test_zero_factor_is_refused`, `apps/units/tests/test_models.py::test_negative_factor_is_refused` | Done | DB-enforced |
| UOM-003 | Cross-dimension conversion is refused | units | `services._require_same_dimension` | `apps/units/tests/test_conversion.py::TestDimensionSafety` | Done | A kg of rice is not a litre |
| UOM-004 | Converting to base and back is lossless at 3 dp | units | `services.convert` | `test_conversion.py::test_round_trip_is_lossless_within_the_declared_precision` | Done | Hypothesis property |
| UOM-005 | Golden cases match hand calculation | units | `services.convert_to_stored_quantity` | `test_conversion.py::TestGoldenCases` | Done | `docs/testing/golden-cases/units-conversion.md` |
| UOM-006 | Packaging and yield are NOT unit conversions | units | seed list, module docstrings | `apps/units/tests/test_models.py::test_packaging_units_are_deliberately_absent` | Done | Phase 1 and Phase 3 respectively |
| UOM-007 | Factors stored once to base; inverses derived | units | `factor_to_base` | `apps/units/tests/test_conversion.py::TestBaseHelpers` | Done | No independent reciprocals to disagree |
| UOM-008 | 12-dp factor precision stores an ounce exactly | units | `FACTOR_PLACES` | `apps/units/tests/test_models.py::test_an_ounce_is_stored_to_full_precision` | Done | Confirmed, not to be reduced |
| MON-001 | Posted amounts store at 3 dp, ROUND_HALF_UP | core | `money.quantize_money` | `apps/core/tests/test_money.py::TestPostedAmountPrecision` | Done | ADR-012 |
| MON-002 | IQD displays at 0 dp; display values are never stored | core | `money.money_for_display` | `test_money.py::TestRendering` | Done | |
| MON-003 | Unit prices and rates keep 6 dp internally | core | `quantize_unit_price`, `quantize_rate` | `test_money.py::TestHigherInternalPrecision` | Done | |
| MON-004 | Money shares no rounding with quantities | core | separate modules and naming | `test_money.py::test_money_precision_is_independent_of_quantity_precision` | Done | |
| MON-005 | Reversals cancel exactly | core | `MONEY_ROUNDING` | `test_money.py::test_rounding_is_symmetric_so_reversals_cancel` | Done | Ties away from zero |
| MON-006 | Allocated lines sum exactly to the source amount | core | `allocation.allocate_proportionally` | `apps/core/tests/test_allocation.py::test_parts_always_sum_to_the_whole` | Done | Largest remainder; Hypothesis property |
| MON-007 | Residual ties break on line order | core | `allocation` sort key | `test_allocation.py::TestSequenceIsTheTieBreak` | Done | Caller must pass a stable order |
| MON-008 | A rate is applied to the total, not line by line | core | `allocation.allocate_by_rate` | `test_allocation.py::test_rate_is_applied_to_the_total_not_line_by_line` | Done | |
| MON-009 | Credit notes mirror their invoice line for line | core | sign handling in `allocation` | `test_allocation.py::test_reversal_is_the_exact_mirror` | Done | Hypothesis property |
| MON-010 | Nearest-250 rounding is off | core | `CASH_ROUNDING_ENABLED` | `test_money.py::TestCashRoundingIsOff` | Done | Tripwire test |
| MON-011 | Cash rounding residual posts to an explicit account | core | `apply_cash_settlement_rounding` returns `(rounded, adjustment)` | `test_money.py::test_the_adjustment_always_reconciles` | Partial | Account seeded in Task 0.6 (ADR-014) |
| MON-012 | Rendered money cannot enter arithmetic | core | `money_display` / `money_audit` / `money_export` return `str` | `test_money.py::TestRendering` | Done | Structural, not conventional |
| MON-013 | Audit and export views expose the stored third decimal | core | `money_audit`, `money_export` | `test_money.py::test_audit_views_expose_the_stored_third_decimal` | Done | |
| MON-014 | Reconciliation compares stored values, never displayed | core | renderers return `str` | `test_money.py::test_reconciliation_must_compare_stored_values` | Done | |
| MON-015 | Allocation requires an explicit unique sequence | core | `AllocationItem.sequence`, `_validate_sequences` | `test_allocation.py::TestSequenceValidation` | Done | Missing, duplicate, negative, non-integer all refused |
| MON-016 | Caller order never changes an allocation | core | sort by sequence before allocating | `test_allocation.py::test_caller_order_does_not_change_the_outcome`, `test_allocation.py::test_shuffling_the_input_never_changes_the_result` | Done | Hypothesis property |
| AUD-001 | Every audited action records actor, reason, and correlation | core | `record_audit_event` | `apps/core/tests/test_audit.py::TestRecording` | Done | Actor from context, not arguments |
| AUD-002 | The audit trail is append-only | core | PostgreSQL trigger, migration `core.0002` | `apps/core/tests/test_audit.py::TestImmutability` | Done | ORM, bulk update, and raw SQL all refused |
| AUD-003 | Events from one unit of work share a correlation id | core | `apps/core/context.py`, middleware | `test_audit.py::TestCorrelation` | Done | Echoed as `X-Correlation-ID` |
| AUD-004 | Audit snapshots preserve Decimal exactness | core | `services._json_safe` | `test_audit.py::test_decimals_are_stored_as_strings_not_floats` | Done | Decimals stored as strings, never floats |
| AUD-005 | Secrets are never captured in a snapshot | core | `NEVER_SNAPSHOT` | `test_audit.py::test_sensitive_fields_are_never_captured` | Done | |
| AUD-006 | Actor identity survives a later rename | core | `actor_label` denormalised | `test_audit.py::test_the_actor_name_is_kept_as_text` | Done | |
| AUD-007 | A user with audit events cannot be deleted | core | `on_delete=PROTECT` | `test_audit.py::test_an_actor_with_events_cannot_be_deleted` | Done | |
| AUD-008 | Mutable master data keeps row history | organizations, units, users | `HistoricalRecords` | `test_audit.py::TestRowHistory` | Done | Password excluded from user history |
| AUD-009 | Audit context never leaks between requests | core | middleware resets in `finally` | `test_audit.py::test_context_does_not_leak_between_requests` | Done | Reset even when the view raises |

## Task 0.7 — permissions, scope, API, idempotency

| ID | Requirement | App | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|---|
| PRM-001 | Twelve named accounting permissions exist | accounting | `Meta.permissions` on `JournalEntry`, `Account`, `CostCenter`, `AccountingPeriod` | `test_permissions.py::TestThePermissionsExist` | Done | Not Django add/change/delete |
| PRM-002 | Roles carry permissions through groups | organizations | `permissions.sync_user_role_groups`, `role:<ROLE>` groups | `test_permissions.py::TestRoleGroupsFollowMemberships` | Done | Recomputed, never incremented |
| PRM-003 | ACCOUNTING_MANAGER holds `reopen_period` | accounting | `ROLE_PERMISSIONS` | `test_permissions.py::test_accounting_manager_may_reopen` | Done | ADR-013 amendment |
| PRM-004 | Branch Manager, Branch Accountant, Cashier, warehouse roles do not | accounting | `ROLE_PERMISSIONS` | `test_permissions.py::test_no_other_role_may_reopen` | Done | Parametrised over every excluded role |
| PRM-005 | Services check permission and scope, never a role name | accounting | `commands.py` | `test_security.py` | Done | No role string appears in a service |
| SCP-001 | Organization scope comes from `OrganizationMembership` only | organizations | `authorization.organization_scope` | `test_permissions.py::test_branch_authority_is_never_organization_authority` | Done | Branch memberships never accumulate |
| SCP-002 | Organization authority reaches every branch in it | organizations | `selectors.accessible_branches` | `test_api.py::TestSoftClosedPeriodOverHttp` | Done | Containment is one-directional |
| SCP-003 | A submitted `organization_id` cannot widen access | organizations | `authorization.resolve_organization` | `test_security.py::test_1_submitting_a_foreign_organization_id_is_refused` | Done | 403, not a silent filter |
| SCP-004 | A submitted `branch_id` cannot widen access | organizations | `authorization.resolve_branch` | `test_security.py::test_2_submitting_a_foreign_branch_id_is_refused` | Done | Same organization, different branch |
| SCP-005 | A foreign account or cost centre cannot be injected | accounting | `commands._scoped_account`, `_scoped_cost_center` | `test_security.py::TestForeignObjectInjection` | Done | Filtered, not fetched-then-checked |
| SCP-006 | Authority is needed at every branch an entry touches | accounting | `commands._require_at_every_branch` | `test_security.py::test_an_entry_spanning_two_branches_needs_authority_at_both` | Done | Not "at least one" |
| SCP-007 | Period acts require organization scope | accounting | `PERMISSION_SCOPE` | `test_security.py::test_9_a_branch_accountant_cannot_close_a_period` | Done | Holds the permission, holds it nowhere |
| API-001 | Commands, not writable CRUD, for posted ledger state | accounting | `apps/accounting/api.py` | `test_api.py` | Done | No PUT; PATCH is drafts only |
| API-002 | The API never reaches the kernel directly | accounting | import boundary | `test_security.py::test_14c_the_api_layer_never_imports_the_kernel_directly` | Done | Architectural test over the AST |
| API-003 | Endpoints authenticate by default | config | `NinjaAPI(auth=django_auth)` | `test_api.py::TestAuthenticationIsRequired` | Done | `/health` is the only exception |
| API-004 | Errors map to 403 / 404 / 409 / 422 | config | `config/api.py` exception handlers | `test_api.py` | Done | Conflict codes listed explicitly |
| API-005 | Money crosses the boundary as exact decimal strings | accounting | `LineIn` str fields, `money_export` | `test_api.py::TestExactDecimalTransport` | Done | Checked against raw JSON, both directions |
| API-006 | API decimals are never grouped or localised | accounting | `money_export` | `test_api.py::test_amounts_are_never_grouped_or_localised` | Done | Technical identity, not display |
| IDM-001 | One economic event, one journal, per organization | accounting | `journal_entry_source_event_unique_per_organization` | `test_source_identity.py::TestTheGuaranteeSurvivesACommit` | Done | Partial unique index, real COMMIT |
| IDM-002 | `source_event` is a closed enum | accounting | `SourceEvent` + `journal_entry_source_event_is_known` | `test_source_identity.py::TestTheEnumIsClosed` | Done | Typos refused by app and database |
| IDM-003 | A source identity is complete or absent | accounting | `validate_source_identity` + check constraint | `test_source_identity.py::TestIdentityIsCompleteOrAbsent` | Done | Manual journals carry none |
| IDM-004 | A retry returns the existing journal | accounting | `post_entry` idempotency key | `test_source_identity.py::test_a_retried_command_returns_the_same_journal` | Done | |
| IDM-005 | A conflicting reuse is a domain error, not an IntegrityError | accounting | `source_event_already_posted` | `test_source_identity.py::test_the_same_event_under_a_different_key_is_a_conflict` | Done | Names the entry that holds it |
| IDM-006 | The same source id is allowed in another organization | accounting | organization in the index | `test_source_identity.py::test_the_same_source_id_is_allowed_in_another_organization` | Done | |
| IDM-007 | POSTED and REVERSED coexist for one document | accounting | `reverse_entry` sets `SourceEvent.REVERSED` | `test_source_identity.py::TestPostedAndReversedCoexist` | Done | |
| IDM-008 | Source identity is immutable once posted | accounting | immutability trigger | `test_source_identity.py::TestSourceIdentityIsImmutable` | Done | |
| ADM-001 | Posted ledger state is read-only in the admin | accounting | `ReadOnlyAdminMixin` | `test_admin_lockdown.py` | Done | For superusers too |
| ADM-002 | The admin URLs refuse, not just the permission methods | accounting | Django admin | `test_admin_lockdown.py::TestTheAdminUrlsRefuse` | Done | POST to change and delete both checked |
| LDG-001 | A posted entry is immutable on every column | accounting | migration `0005` allowlist trigger | `test_source_identity.py::test_no_other_posted_column_can_be_rewritten_either` | Done | **Fixes a Task 0.6 defect** |
| LDG-002 | A draft promoted to POSTED is balanced | accounting | trigger `accounting_journalentry_balance_on_post` | `test_api.py::test_an_unbalanced_draft_is_refused_at_posting_not_at_creation` | Done | The 0002 trigger fires on lines only |
| LDG-003 | Drafts consume no journal number | accounting | partial unique + check constraint | `test_api.py::test_create_amend_post` | Done | Numbering stays gapless |
| LDG-004 | Soft-closed posting needs authority and a reason | accounting | `_require_soft_close_override` | `test_api.py::TestSoftClosedPeriodOverHttp` | Done | Override audited separately |
| LDG-005 | A reopening records actor, org, period, both states, reason | accounting | `reopen_accounting_period` | `test_security.py::test_the_reopening_records_actor_organization_period_states_and_reason` | Done | |

## Task 0.8 — Phase 0 exit gate

| ID | Requirement | App | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|---|
| EXIT-001 | ACCOUNTANT holds no structural authority by default | accounting | `ROLE_PERMISSIONS` | `test_permissions.py::test_the_accountant_holds_no_structural_authority` | Done | Chart, cost centres, and all period acts are Manager/Owner |
| EXIT-002 | OWNER means proprietor, not passive investor | — | ADR-016 amendment | — | Documented | No investor role invented; boundary recorded |
| EXIT-003 | Out-of-scope objects answer 404 | organizations | `OutOfScope(ObjectDoesNotExist)` | `test_security.py::TestCrossOrganization`, `TestCrossBranch` | Done | Same code and wording as a missing row |
| EXIT-004 | In-scope without authority answers 403 | organizations | `PermissionMissing(PermissionDenied)` | `test_security.py::test_9_a_branch_accountant_cannot_close_a_period` | Done | Reaching is weaker than scope |
| EXIT-005 | Idempotency keys are unique per organization | accounting | `journal_entry_idempotency_key_unique_per_organization` | `test_idempotency.py::TestKeysAreScopedToTheOrganization` | Done | **Fixes a cross-tenant leak** |
| EXIT-006 | A replay is verified against the request | accounting | `idempotency_fingerprint`, `_replay` | `test_idempotency.py::TestSameKeyDifferentRequest` | Done | `idempotency_key_conflict` |
| EXIT-007 | A key cannot reach another organization's journal | accounting | org-scoped lookup + selector | `test_idempotency.py::test_a_key_cannot_be_used_to_discover_another_organizations_journal` | Done | |
| EXIT-008 | `source_document_id` carries any identifier type | accounting | `CharField(max_length=64)` | — | Verified | Unchanged. int, UUID, or external ref (ADR-017) |
| EXIT-009 | Losing one of two memberships keeps the role, drops the scope | organizations | `sync_user_role_groups` | `test_permissions.py::TestMultiMembershipRecomputation` | Done | Branch and organization variants |
| EXIT-010 | Global permissions never substitute for object scope | organizations | `authorization.py` | `test_permissions.py::test_a_global_permission_never_substitutes_for_scope` | Done | |
| EXIT-011 | Native foundation screens exist inside the shell | organizations, units, users, core | `urls.py` + `navigation.py` | `apps/core/tests/test_foundation_screens.py` | Done | Org, branch, access, users, units, audit |
| EXIT-012 | A fresh database migrates from zero and seeds | all | migrations + seed commands | Verified manually on `khan_mandi_freshcheck` | Verified | Manual, not automated. 10 units, 46 accounts, 6 cost centres, 8 role groups |
| EXIT-013 | Seeding survives a non-UTF-8 console | core, units | `apps.core.console.SeedCommand` | `tests/test_phase_0_exit.py::test_seeding_survives_a_console_that_cannot_render_arabic` | Done | **Fixes a fresh-install failure** |
| EXIT-014 | The foundations cooperate end to end | all | — | `tests/test_phase_0_exit.py::TestTheFoundationsCooperate` | Done | Services and API, no ORM shortcuts |

## Phase 1 — Inventory (specified by Task 1.0, none implemented)

Task 1.0's decisions were **approved with amendments on 2026-08-09**; the
rows below reflect the approved design. Status is **Specified** until the
owning task implements it;
the "Implementation" column names what is proposed, and the "Test" column what
must exist before the owning task may close. See
`docs/tasks/task-1-0-inventory-domain-spec.md` and
`docs/invariants/inventory-invariants.md`.

### Phase 1 acceptance criteria

These `AT-*` identifiers appear nowhere in the repository or in the source
documents. They are **established here** from the descriptions supplied with
Task 1.0, not referenced from an earlier artefact.

| AT | Meaning |
|---|---|
| AT-002 | Inventory value reconciles to the general ledger |
| AT-007 | Stock balance rebuilds exactly from the movement ledger |
| AT-008 | Scope and privacy — no cross-tenant read or write |
| AT-009 | Idempotency — one economic event, one effect |
| AT-011 | Historical effective data is not silently restated |
| AT-012 | Import atomicity |

### Requirements

| ID | Requirement | Implementation | Test | Task | AT | Status |
|---|---|---|---|---|---|---|

## How to read the evidence column

Every citation names a file and a test that exist. `tests/test_traceability.py`
proves it by parsing this document against the real suite, so a row can no
longer cite a test that was never written — which is what sixty rows from Tasks
1.1 and 1.2 did until the evidence was reconciled. Those rows were written as
*intentions*, before the tests, and the tests that eventually arrived took
different and better names; nothing failed, because nothing checked.

A citation is `path/to/test_file.py::Name`, where `Name` is a test function or
the class that holds it. A class is cited when the whole class is the evidence
and singling out one method would understate it. Paths are given in full: a
bare `test_models.py` is three different files.

Four statuses carry a promise:

- **Done** — an automated test proves it. The only status that obliges a
  citation, and the reason the others exist.
- **Verified** — inspected, not exercised. A non-null column or a unique
  constraint whose enforcement is structural and whose test would only restate
  the schema. The cell says what enforces it.
- **Partial** — covered narrower than the requirement is written. The cell says
  where the gap is; `INV-034` is the example.
- **Deferred** — genuinely not built, with the reason in the cell. `INV-004`
  and `INV-039` turned out to describe work this architecture does not do at
  all, which is worth recording rather than deleting.


| INV-001 | Item code unique per organization; archived codes reserved | `UniqueConstraint(organization, code)` | `apps/inventory/tests/test_master_data.py::TestItemCode` | 1.1 | AT-008 | Done |
| INV-002 | A foreign organization's item cannot be injected | `_scoped_item` resolver | `apps/inventory/tests/test_screens_and_api.py::test_a_foreign_item_id_is_a_404`, `apps/inventory/tests/test_ledger.py::test_a_foreign_item_is_refused` | 1.1 | AT-008 | Done |
| INV-003 | A foreign branch's warehouse is unreachable (404) | `resolve_warehouse` via Phase 0 authorization | `apps/inventory/tests/test_scope_and_permissions.py::test_a_foreign_branch_warehouse_is_a_404` | 1.1 | AT-008 | Done |
| INV-004 | Base UoM dimension validated against the entered unit | Reuses `units.services._require_same_dimension` | — an item package is not a unit conversion (UOM-006), so no second dimension is entered to validate; `create_item` takes the base unit directly and `add_item_conversion` resolves to it | 1.1 | | Deferred |
| INV-005 | Base UoM immutable once movements exist | Service guard | `apps/inventory/tests/test_ledger.py::TestPostedHistoryFreezesMasterData`, `apps/inventory/tests/test_native_workflows.py::test_a_posted_base_unit_cannot_be_swapped_by_posting_one` | 1.1 | | Done |
| INV-006 | Fixed package conversion applies exactly | `ItemUnitConversion` `FIXED` | `apps/inventory/tests/test_master_data.py::test_a_fixed_conversion_resolves_directly_to_base`, `apps/inventory/tests/test_operations.py::test_a_fixed_package_converts_arithmetically` | 1.1 | | Done |
| INV-007 | Variable package requires a measured base quantity | `VARIABLE` + `measured_quantity_required` | `apps/inventory/tests/test_operations.py::test_a_variable_package_requires_the_measured_quantity` | 1.1 | | Done |
| INV-008 | Overlapping conversion periods refused | `EXCLUDE USING gist` | `apps/inventory/tests/test_master_data.py::test_overlapping_effective_periods_are_impossible` | 1.1 | | Done |
| INV-009 | Conversion snapshot stays historical after the master changes | Factor + version stored on the movement | `apps/inventory/tests/test_transfers.py::test_the_dispatch_conversion_snapshot_survives_a_new_factor`, `apps/inventory/tests/test_ledger.py::test_a_used_conversion_cannot_be_edited_in_place` | 1.2 | AT-011 | Done |
| INV-010 | No float in inventory storage or transport | `quantity.py` / `money.py`; string API decimals | `apps/inventory/tests/test_imports_and_projection.py::test_decimal_values_never_pass_through_float`, `apps/inventory/tests/test_stock_screens_and_api.py::test_decimals_cross_the_wire_as_strings` | 1.1 | | Done |
| INV-011 | Arabic locale does not change technical decimal strings | Locale-independent rendering | `apps/inventory/tests/test_master_data.py::TestFactorsAreLocaleIndependent`, `apps/inventory/tests/test_stock_screens_and_api.py::test_a_technical_decimal_keeps_its_point_under_arabic` | 1.1 | | Done |
| INV-012 | Posted stock movements are immutable | Allowlist trigger, per `accounting/0005` | `apps/inventory/tests/test_ledger.py::TestTheLedgerIsAppendOnly` | 1.2 | | Done |
| INV-013 | Every movement carries the full required column set | Non-null columns | — enforced by the non-null columns on `StockMovement`; no test asserts the column list itself, and a migration check would be the honest test | 1.2 | | Verified |
| INV-014 | Valuation key is `(warehouse, item, lot)` | `UniqueConstraint` on `StockBalance` | `apps/inventory/tests/test_ledger.py::test_the_null_lot_balance_is_unique`, `apps/inventory/tests/test_operations.py::test_a_duplicate_valuation_key_is_rejected` | 1.2 | | Done |
| INV-015 | Moving weighted average — all 18 cases | Valuation engine | `test_ledger.py::TestMovingWeightedAverage` | 1.2 | | Done |
| INV-016 | Quantity zero implies value zero | Full-depletion rule | `apps/inventory/tests/test_operations.py::test_full_depletion_leaves_zero_quantity_and_zero_value`, `apps/inventory/tests/test_valuation_properties.py::test_zero_quantity_implies_zero_value_after_any_single_step` | 1.2 | AT-007 | Done |
| INV-017 | `StockBalance` rebuilds exactly from the ledger | Rebuild command | `apps/inventory/tests/test_ledger.py::TestRebuild` | 1.2 | AT-007 | Done |
| INV-018 | Negative stock refused by default | Service check inside the lock + trigger | `apps/inventory/tests/test_ledger.py::TestNegativeStockIsRefused` | 1.2 | | Done |
| INV-019 | Concurrent issues cannot create negative stock | `select_for_update` in deterministic order | `apps/inventory/tests/test_ledger_concurrency.py::TestConcurrentIssues` | 1.2 | | Done |
| INV-020 | Negative-stock override needs permission, reason, actor, audit | `inventory.override_negative_stock` | `apps/inventory/tests/test_ledger.py::test_even_the_owner_is_refused_and_no_role_holds_the_override` | 1.2 | AT-008 | Done |
| INV-021 | Closed-period movements refused | Reuses `validate_period_accepts_postings` | `apps/inventory/tests/test_ledger.py::TestPeriodAndWarehouseState` | 1.2 | AT-011 | Done |
| INV-022 | Backdated valuation follows posting order, not effective date | Documented policy + test | `apps/inventory/tests/test_ledger.py::test_case_13_a_backdated_posting_does_not_reprice_what_came_before` | 1.2 | AT-011 | Done |
| INV-023 | COMMIT-boundary constraints exercised | `transaction=True` tests | `apps/inventory/tests/test_ledger_concurrency.py`, `apps/inventory/tests/test_opening_concurrency.py`, `apps/inventory/tests/test_count_concurrency.py` — all `transaction=True` | 1.2 | | Done |
| INV-024 | Audit captures authoritative before/after state | `record_audit_event` with DB re-read | `apps/inventory/tests/test_native_workflows.py::test_edit_records_a_before_that_differs_from_the_after` | 1.2 | | Done |
| INV-025 | No hard-coded account ids in inventory posting | `AccountRole` + `AccountMapping` | `apps/inventory/tests/test_business_date.py::test_a_bare_kernel_posting_records_no_account_rather_than_inventing_one`, `apps/inventory/tests/test_transfers.py::test_a_journalled_posting_names_an_account_for_every_dinar` | 1.3 | | Done |
| INV-026 | Opening value equals its journal entry | Atomic opening posting | `apps/inventory/tests/test_opening_stock.py::test_line_movement_and_journal_all_carry_the_same_value` | 1.3 | AT-002 | Done |
| INV-027 | Inventory control reconciles to inventory valuation | Reconciliation report | `apps/inventory/tests/test_opening_stock.py::TestReconciliation`, `apps/inventory/tests/test_reports_and_exports.py::test_gl_reconciliation_agrees_and_offers_no_repair` | 1.3 | AT-002 | Done |
| INV-028 | Duplicate source event cannot double-post | Source identity (ADR-017) | `apps/inventory/tests/test_opening_concurrency.py::test_a_concurrent_duplicate_post_creates_one_economic_event` | 1.3 | AT-009 | Done |
| INV-029 | Same key + changed payload conflicts | Idempotency fingerprint | `apps/inventory/tests/test_opening_stock.py::test_the_same_key_with_a_changed_payload_conflicts` | 1.3 | AT-009 | Done |
| INV-030 | Same key in another organization is independent | Org-scoped key | `apps/inventory/tests/test_ledger.py::test_the_same_key_in_another_organization_is_independent` | 1.3 | AT-009 | Done |
| INV-031 | Reversal restores quantity and value exactly | `REVERSAL` at the original's value | `apps/inventory/tests/test_opening_stock.py::test_a_successful_reversal_restores_stock_and_gl_exactly` | 1.4 | | Done |
| INV-032 | `RETURN_IN` values at the original issue cost | Link to the issuing movement | `apps/inventory/tests/test_operations.py::test_a_partial_return_uses_the_original_issue_cost` | 1.4 | | Done |
| INV-033 | Transfer dispatch reconciles to receipt plus shortage | In-transit accounting | `apps/inventory/tests/test_transfers.py::test_receipts_plus_shortage_equal_the_dispatch` | 1.5 | AT-002 | Done |
| INV-034 | Inter-branch transfer needs authority at both branches | `_require_at_every_branch` pattern | `apps/accounting/tests/test_security.py::test_an_entry_spanning_two_branches_needs_authority_at_both` covers the journal; the transfer's own dispatch/receive permissions are checked at one branch each (`apps/inventory/tests/test_transfers.py::TestPermissions`), which is weaker than the row claims | 1.5 | AT-008 | Partial |
| INV-035 | Posting to a frozen warehouse refused | `freeze_state` guard | `apps/inventory/tests/test_waste_counts_adjustments.py::test_a_frozen_warehouse_refuses_every_posting` | 1.6 | | Done |
| INV-036 | Conducting and approving a count are separate permissions | Two permissions | `apps/inventory/tests/test_scope_and_permissions.py::test_the_manager_holds_both_count_permissions`, `apps/inventory/tests/test_waste_counts_adjustments.py::test_the_conductor_cannot_approve_their_own_count` | 1.6 | | Done |
| INV-037 | Import rollback is atomic | Import boundary | `apps/inventory/tests/test_imports_and_projection.py::test_one_invalid_row_stops_the_whole_batch` | 1.7 | AT-012 | Done |
| INV-038 | Located quantities plus the unlocated remainder equal the warehouse quantity | `locations.py` + `verify_locations` | `test_locations.py::TestTheInvariant` | 1.7B | AT-007 | Done |
| INV-039 | A fresh database receives inventory reference data | Seed command | — inventory ships no reference data of its own: reason codes, categories and package units are all organization-created. `seed_inventory_demo` is demo data, not reference data, and must never run in production | 1.7 | | Deferred |
| INV-040 | No writable CRUD bypasses the posting services | Command API + read-only admin | `apps/inventory/tests/test_stock_screens_and_api.py::TestThereIsNoWritePath`, `apps/inventory/tests/test_stock_screens_and_api.py::test_the_ledger_models_are_registered_read_only` | 1.1 | AT-008 | Done |
| INV-041 | Category codes unique per organization; cycles rejected | Service guard + constraint | `apps/inventory/tests/test_master_data.py::test_code_is_unique_per_organization`, `apps/inventory/tests/test_master_data.py::test_a_category_cannot_be_moved_beneath_itself` | 1.1 | | Done |
| INV-042 | Category depth never exceeds 3, including on re-parent | Service guard | `apps/inventory/tests/test_master_data.py::test_a_fourth_level_is_refused`, `apps/inventory/tests/test_master_data.py::test_a_move_that_would_push_children_too_deep_is_refused` | 1.1 | | Done |
| INV-043 | A category with items cannot acquire children | Service guard | `apps/inventory/tests/test_master_data.py::test_a_category_holding_items_cannot_acquire_children` | 1.1 | | Done |
| INV-044 | A category with children cannot receive items | Service guard | `apps/inventory/tests/test_master_data.py::test_an_item_cannot_sit_on_a_category_with_children` | 1.1 | | Done |
| INV-045 | Item code canonicalised `strip().upper()` before storage | `create_item` / `update_item` | `apps/inventory/tests/test_master_data.py::test_the_code_is_canonicalised` | 1.1 | | Done |
| INV-046 | `tracks_expiry` requires `tracks_lots` | Check constraint | `apps/inventory/tests/test_master_data.py::test_the_database_refuses_expiry_without_lots` | 1.1 | | Done |
| INV-047 | `FINISHED_GOOD` exists and is not a menu item | `ItemType` enum | `apps/inventory/tests/test_master_data.py::test_a_finished_good_is_an_inventory_item_not_a_menu_item` | 1.1 | | Done |
| INV-048 | A package unit carries no universal factor | `PackageUnit` has no factor field | `apps/inventory/tests/test_master_data.py::TestPackageUnitsCarryNoFactor` | 1.1 | | Done |
| INV-049 | Conversions resolve directly to base; no chaining | `ItemPackageConversion` | `apps/inventory/tests/test_master_data.py::test_two_packages_of_one_item_both_resolve_to_base_without_chaining` | 1.1 | | Done |
| INV-050 | One active default purchase package per item | Partial unique index | `apps/inventory/tests/test_master_data.py::test_only_one_default_purchase_package_per_item` | 1.1 | | Done |
| INV-051 | Overlapping conversion periods rejected | `EXCLUDE USING gist` | duplicate of INV-008 — `apps/inventory/tests/test_master_data.py::test_overlapping_effective_periods_are_impossible` | 1.1 | | Done |
| INV-052 | `BranchItemSetting` unique per (branch, item) | `UniqueConstraint` | — `branch_item_setting_unique` is the enforcement and `set_branch_item_setting` the only writer; no test asserts the constraint directly | 1.1 | | Verified |
| INV-053 | `SELECTED` warehouse scope restricts access | `BranchMembership.warehouse_scope_mode` | `apps/inventory/tests/test_scope_and_permissions.py::test_selected_mode_restricts_to_the_listed_warehouses` | 1.1 | AT-008 | Done |
| INV-054 | `ALL` warehouse scope includes newly created warehouses | Same | `apps/inventory/tests/test_scope_and_permissions.py::test_all_mode_includes_a_warehouse_created_later` | 1.1 | AT-008 | Done |
| INV-055 | A warehouse selection cannot cross branches | Service guard | `apps/inventory/tests/test_scope_and_permissions.py::test_a_selection_cannot_cross_branches` | 1.1 | AT-008 | Done |
| INV-056 | A system `IN_TRANSIT` warehouse is protected from normal users | Service guard | `apps/inventory/tests/test_scope_and_permissions.py::test_the_database_refuses_a_system_flag_on_an_ordinary_warehouse`, `apps/inventory/tests/test_transfers.py::test_the_in_transit_warehouse_cannot_be_chosen` | 1.1 | | Done |
| INV-057 | Warehouse code unique per branch, canonical uppercase | `UniqueConstraint` | `apps/inventory/tests/test_scope_and_permissions.py::test_code_is_unique_per_branch`, `apps/inventory/tests/test_scope_and_permissions.py::test_the_code_is_canonicalised` | 1.1 | | Done |
| INV-058 | A count approver is never the conductor | `approver_id != conductor_id` | `apps/inventory/tests/test_waste_counts_adjustments.py::test_the_conductor_cannot_approve_their_own_count`, `apps/inventory/tests/test_waste_counts_adjustments.py::test_maker_checker_is_a_database_constraint_too` | 1.6 | | Done |
| INV-059 | A positive count gain never posts at zero value | Explicit unit cost required | `apps/inventory/tests/test_waste_counts_adjustments.py::test_a_zero_book_gain_needs_an_approved_unit_cost` | 1.6 | | Done |
| INV-060 | Source identity normalised centrally; `"145 "` == `"145"` | Accounting service | duplicate of INV-071 — `apps/inventory/tests/test_ledger.py::TestSourceIdentityCanonicalisation` | 1.2 | AT-009 | Done |
| INV-061 | A reversal that decreases stock passes the availability check | Posting service | `apps/inventory/tests/test_operations.py::test_a_receipt_reversal_respects_availability` | 1.4 | | Done |
| INV-062 | Every report names its cutoff semantics | Report contract | `apps/inventory/tests/test_reports_and_exports.py::test_the_mode_is_shown_on_every_historical_screen` | 1.7 | AT-011 | Done |
| INV-063 | A permission is carried by a post held **in the target organization** | `roles_in_organization` + `roles_granting` | `test_permission_provenance.py::TestTheProvenanceRule` | 1.1 | AT-008 | Done |
| INV-064 | A global group or direct user permission authorizes no organization | Same | `test_permission_provenance.py::test_a_hand_made_group_authorizes_no_organization` | 1.1 | AT-008 | Done |
| INV-065 | Organization *authority* comes only from an `OrganizationMembership` role | `organization_authority_roles` | `test_permission_provenance.py::TestOrganizationAuthorityProvenance` | 1.1 | AT-008 | Done |
| INV-066 | Button visibility never differs from what the write allows | `organizations_with_permission` | `test_permission_provenance.py::TestBulkAnswersMatchTheSingleCheck` | 1.1 | AT-008 | Done |
| INV-067 | Master-data screens write through services only; no `form.save()` | `apps/inventory/views.py` | `test_native_workflows.py::TestTheWritePathIsStructurallySafe` | 1.1 | | Done |
| INV-068 | A hidden action refuses a direct POST on its own merits | `InventoryWriteView.authorize` | `test_native_workflows.py::TestButtonsAreNotTheProtection` | 1.1 | AT-008 | Done |
| INV-069 | An unused conversion is corrected in place; a used one must be versioned | `update_item_conversion` | `test_native_workflows.py::test_editing_corrects_an_unused_factor` | 1.1 | AT-011 | Done |
| INV-070 | An archived warehouse stays readable and reactivatable | `readable_warehouses` | `test_native_workflows.py::test_create_edit_archive_and_reactivate` | 1.1 | | Done |
| INV-071 | Source identity is canonicalised centrally; `"145 "` == `"145"` | `apps/core/source_identity.py` | `test_ledger.py::TestSourceIdentityCanonicalisation` | 1.2 | AT-009 | Done |
| INV-072 | `source_document_id` is NOT case-folded — it is the supplier's vocabulary | Same | `test_ledger.py::test_case_is_folded_on_our_vocabulary_and_not_on_theirs` | 1.2 | AT-009 | Done |
| INV-073 | A retry with the same payload returns the original posting | `_replay` + `request_fingerprint` | `test_ledger.py::TestIdempotency` | 1.2 | AT-009 | Done |
| INV-074 | A key reused with a changed payload is `idempotency_key_conflict` | Same | `test_ledger.py::test_a_changed_payload_is_a_conflict` | 1.2 | AT-009 | Done |
| INV-075 | The fingerprint excludes the server clock, so retries can match | `request_fingerprint` | `test_ledger.py::test_the_same_payload_returns_the_original` | 1.2 | AT-009 | Done |
| INV-076 | Effect keys are unique per posting, in service and in database | `UniqueConstraint(entry, effect_key)` | `test_ledger.py::test_the_database_refuses_a_duplicate_effect_key_too` | 1.2 | AT-009 | Done |
| INV-077 | Quantity zero implies value zero, by construction and by constraint | `apply_outbound` + check constraints | `test_ledger.py::test_full_depletion_absorbs_the_exact_remaining_value` | 1.2 | AT-002 | Done |
| INV-078 | A full depletion absorbs the exact remaining value | Same | `test_valuation_properties.py` | 1.2 | AT-002 | Done |
| INV-079 | Negative stock is refused for everyone in Task 1.2 | `_require_available` | `test_ledger.py::TestNegativeStockIsRefused` | 1.2 | | Done |
| INV-080 | A reversal mirrors quantity and value, not today's average | `apply_reversal` | `test_ledger.py::test_a_reversal_mirrors_the_original_exactly` | 1.2 | AT-011 | Done |
| INV-081 | A reversal that would go negative is refused | `reverse_stock_entry` | `test_ledger.py::test_a_reversal_that_would_go_negative_is_refused` | 1.2 | | Done |
| INV-082 | A reversal cannot be reversed, and nothing is reversed twice | Same | `test_ledger.py::TestReversal` | 1.2 | | Done |
| INV-083 | `StockMovement` is insert-only at the database | Trigger `stock_movement_is_insert_only` | `test_ledger.py::TestTheLedgerIsAppendOnly` | 1.2 | | Done |
| INV-084 | A ledger entry is immutable except for its reversal back-link | Trigger `stock_entry_is_immutable` | `test_ledger.py::test_an_entry_cannot_be_edited` | 1.2 | | Done |
| INV-085 | The null-lot balance key is unique (`NULLS NOT DISTINCT`) | `stock_balance_key_unique` | `test_ledger.py::test_the_null_lot_balance_is_unique` | 1.2 | | Done |
| INV-086 | Concurrent issues cannot oversell | Advisory lock per stock key | `test_ledger_concurrency.py::TestConcurrentIssues` | 1.2 | | Done |
| INV-087 | Concurrent first receipts create exactly one balance row | Same | `test_ledger_concurrency.py::TestConcurrentFirstReceipt` | 1.2 | | Done |
| INV-088 | Multi-key events lock in canonical order and never deadlock | `_StockKey.sort_key` | `test_ledger_concurrency.py::TestDeterministicLockOrder` | 1.2 | | Done |
| INV-089 | Posting requires an OPEN period; SOFT_CLOSED and CLOSED refuse | `_validate_period_is_open` | `test_ledger.py::TestPeriodAndWarehouseState` | 1.2 | | Done |
| INV-090 | A frozen stock position refuses postings | `_check_warehouse_is_not_frozen` | `test_ledger.py::test_a_frozen_position_refuses_postings` | 1.2 | | Done |
| INV-091 | Replaying the ledger reproduces the projection exactly | `reconciliation.verify_organization` | `test_ledger.py::TestRebuild` | 1.2 | AT-007 | Done |
| INV-092 | A corrupted projection is detected and never silently repaired | `verify_stock_ledger` | `test_ledger.py::test_the_verify_command_reports_and_refuses_to_repair` | 1.2 | AT-007 | Done |
| INV-093 | Item identity fields freeze once movements exist | `_item_has_movements` | `test_ledger.py::TestPostedHistoryFreezesMasterData` | 1.2 | AT-011 | Done |
| INV-094 | A conversion used by a posted movement must be versioned, not edited | `_conversion_has_movements` | `test_ledger.py::test_a_used_conversion_cannot_be_edited_in_place` | 1.2 | AT-011 | Done |
| INV-095 | No API path writes a stock movement | `config.api` routing table | `test_stock_screens_and_api.py::TestThereIsNoWritePath` | 1.2 | | Done |
| INV-096 | Cost is a separate permission from quantity, and omitted not blanked | `may_see_cost` | `test_stock_screens_and_api.py::test_a_storekeeper_sees_quantity_and_no_cost_at_all` | 1.2 | AT-008 | Done |
| INV-097 | Lot required when tracked, prohibited when not; average is per lot | `_validate_lot` | `test_ledger.py::TestLots` | 1.2 | | Done |
| INV-098 | Expired lots cannot be issued, but can be wasted | `_validate_lot_is_not_expired` | `test_ledger.py::test_an_expired_lot_cannot_be_issued` | 1.2 | | Done |
| INV-099 | `ValuationAllocation` stays empty under moving average | Kernel writes none | `test_ledger.py::test_no_allocation_is_fabricated_under_moving_average` | 1.2 | AT-002 | Done |

## Not yet mapped

The SRS has not been added to this repository. `docs/requirements/SRS.md` is
referenced by `CLAUDE.md` but does not exist. Until it is supplied, requirement
IDs above are local to the bootstrap and are not traceable to a business
source.
| INV-100 | AccountRole vocabulary seeded, system codes immutable | `accounting/0008`, trigger | `test_account_mappings.py::TestTheRoleVocabulary` | 1.3 | | Done |
| INV-101 | Posting rules refer to role codes, never account ids | `apps/inventory/opening.py` | `test_opening_stock.py::test_source_identity_uses_the_immutable_public_id` | 1.3 | | Done |
| INV-102 | Organization mapping: same-org postable active account, no overlap | `create_account_mapping` | `test_account_mappings.py::TestOrganizationMappings` | 1.3 | | Done |
| INV-103 | Used mappings immutable; corrections close and version | `mapping_is_used` | `test_account_mappings.py::test_an_unused_mapping_may_be_amended_or_archived` | 1.3 | | Done |
| INV-104 | Resolver precedence item → nearest ancestor → default → unmapped | `resolve_inventory_account` | `test_inventory_account_mappings.py::TestResolverPrecedence` | 1.3 | | Done |
| INV-105 | Reclassification guard over standing stock, all three doors | `apps/inventory/accounts.py` | `test_inventory_account_mappings.py::TestTheReclassificationGuard` | 1.3 | | Done |
| INV-106 | Opening lifecycle DRAFT→SUBMITTED→POSTED→REVERSED, maker-checker | `apps/inventory/opening.py` | `test_opening_stock.py::TestMakerChecker` | 1.3 | | Done |
| INV-107 | Opening posts stock, valuation, and journal in one transaction | `post_opening_document` | `test_opening_stock.py::test_a_missing_mapping_rolls_the_whole_posting_back` | 1.3 | AT-002 | Done |
| INV-108 | Opening value equals its journal exactly, per account group | grouped stored sums | `test_opening_stock.py::test_grouped_debits_when_items_resolve_to_different_accounts` | 1.3 | AT-002 | Done |
| INV-109 | Opening is the first movement for its valuation keys | history check under locks | `test_opening_stock.py::test_a_key_with_prior_movement_history_is_refused` | 1.3 | | Done |
| INV-110 | Gapless opening numbers, assigned only at posting | `_next_document_number` | `test_opening_stock.py::test_document_numbering_is_gapless_across_a_failed_attempt` | 1.3 | | Done |
| INV-111 | Source identity uses the immutable public id; effect keys use line uids | `post_opening_document` | `test_opening_stock.py::test_the_effect_key_is_the_stable_line_identity` | 1.3 | AT-009 | Done |
| INV-112 | Whole-document reversal mirrors stock and GL exactly, availability applies | `reverse_opening_document` | `test_opening_stock.py::TestReversal` | 1.3 | | Done |
| INV-113 | Posted opening and its lines are database-immutable | triggers, `inventory/0006` | `test_opening_stock.py::test_a_posted_document_is_immutable_at_the_database` | 1.3 | | Done |
| INV-114 | Inventory-to-GL reconciliation by the account history entered | `verify_inventory_against_gl` | `test_opening_stock.py::TestReconciliation` | 1.3 | AT-002 | Done |
| INV-115 | Reconciliation reports, never repairs | `verify_inventory_accounting` | `test_opening_stock.py::test_the_management_command_reports_and_exits_nonzero_on_mismatch` | 1.3 | | Done |
| INV-116 | Combined posting lock order is fixed and deadlock-free | ADR-019, `opening.py` docstring | `test_opening_concurrency.py` | 1.3 | | Done |
| INV-117 | Concurrent duplicate post yields one economic event | document row lock | `test_opening_concurrency.py::test_a_concurrent_duplicate_post_creates_one_economic_event` | 1.3 | AT-009 | Done |
| INV-118 | Mapping authority is organization provenance; overrides share it | `MANAGE_ACCOUNT_MAPPINGS` | `test_account_mappings.py::TestMappingAuthorization`, `test_opening_screens.py::TestProvenanceRegression` | 1.3 | AT-008 | Done |
| INV-119 | Opening cost fields follow `view_valuation`, omitted not blanked | `_serialize_opening` | `test_opening_api.py::TestDecimalsAndCostVisibility` | 1.3 | AT-008 | Done |
| INV-120 | Negative-stock override granted to no default role while disabled | `NEGATIVE_STOCK_OVERRIDE_ENABLED` | `test_ledger.py::test_even_the_owner_is_refused_and_no_role_holds_the_override` | 1.3 | | Done |
| INV-121 | Period validation uses the business date, not the calendar date | `_validate_period_is_open` | `test_business_date.py::TestPeriodValidationUsesTheBusinessDate` | 1.4 | | Done |
| INV-122 | A submitted document's business date cannot silently change | submission snapshot | `test_business_date.py::TestTheOpeningSnapshotIsStable` | 1.4 | | Done |
| INV-123 | Return-to-draft releases the snapshot; resubmission recalculates | `return_opening_to_draft` | `test_business_date.py::test_return_to_draft_releases_the_snapshot_and_resubmission_recalculates` | 1.4 | | Done |
| INV-124 | Postings hold the mapping lock shared; mutations exclusively | `apps/core/locks.py` | `test_mapping_concurrency.py` | 1.4 | | Done |
| INV-125 | A mapping mutation cannot race a posting into stranded value | `begin_mapping_mutation` | `test_mapping_concurrency.py::TestMappingChangeCannotRaceWithPosting` | 1.4 | | Done |
| INV-126 | Shared locks still allow concurrent postings | shared advisory lock | `test_mapping_concurrency.py::test_two_postings_overlap_rather_than_serialising` | 1.4 | | Done |
| INV-127 | The global lock order does not deadlock | ADR-019 §6 | `test_mapping_concurrency.py::TestTheGlobalLockOrderDoesNotDeadlock` | 1.4 | | Done |
| INV-128 | Every value-bearing movement carries its control account | `_control_account_for` | `test_business_date.py::TestTheMovementCarriesItsControlAccount` | 1.4 | | Done |
| INV-129 | A receipt into standing stock preserves the control account | `_control_account_for` | `test_operations.py::TestControlAccountContinuity` | 1.4 | | Done |
| INV-130 | Emptying a position releases its control-account identity | `_save_position` | `test_operations.py::test_emptying_the_position_releases_the_account` | 1.4 | | Done |
| INV-131 | Receipt: Dr control, Cr GRNI, grouped per account | `_plan_receipt` | `test_operations.py::TestReceiptPosting` | 1.4 | AT-002 | Done |
| INV-132 | Issue: Dr consumption, Cr the account the stock is in | `_plan_issue` | `test_operations.py::TestIssue` | 1.4 | AT-002 | Done |
| INV-133 | Issue cost is the moving average; no entered cost accepted | `add_line` | `test_operations.py::test_a_user_supplied_unit_cost_is_refused` | 1.4 | | Done |
| INV-134 | Return valued from the original issue, not today's average | `_plan_return` | `test_operations.py::TestReturnIn` | 1.4 | | Done |
| INV-135 | The final return takes the exact remaining value, no residual | `_plan_return` | `test_operations.py::test_the_final_return_takes_the_exact_remaining_value` | 1.4 | | Done |
| INV-136 | Cumulative returns cannot exceed the issue | `returnable` | `test_operations.py::test_cumulative_returns_cannot_exceed_the_issue` | 1.4 | | Done |
| INV-137 | A return reuses the original accounts and cost centre | `_plan_return` | `test_operations.py::test_todays_mapping_is_not_used_for_the_return` | 1.4 | | Done |
| INV-138 | An issue with active returns cannot be reversed | `reverse_document` | `test_operations.py::test_an_issue_with_active_returns_cannot_be_reversed` | 1.4 | | Done |
| INV-139 | Reversal availability applies to receipts and returns | `reverse_stock_entry` | `test_operations.py::TestReversal` | 1.4 | | Done |
| INV-140 | Gapless numbering per type and business year; failures burn none | `_next_document_number` | `test_operations.py::TestNumberingAndIdempotency` | 1.4 | | Done |
| INV-141 | Source identity uses the immutable public id and line uid | `post_document` | `test_operations.py::test_source_identity_uses_the_immutable_public_id` | 1.4 | AT-009 | Done |
| INV-142 | Posted documents and lines are database-immutable | triggers, `inventory/0010` | `test_operations.py::test_a_posted_receipt_is_immutable_at_the_database` | 1.4 | | Done |
| INV-143 | A document id cannot cross between type series | route-bound type | `test_operations_api_and_screens.py::test_a_document_id_cannot_cross_between_series` | 1.4 | AT-008 | Done |
| INV-144 | Cost is omitted for callers without view_valuation | `_serialize_document` | `test_operations_api_and_screens.py::TestReceiptApi`, `test_operations_api_and_screens.py::test_a_storekeeper_sees_no_recorded_cost` | 1.4 | AT-008 | Done |
| INV-145 | Navigation offers only screens that resolve | `apps/core/navigation.py` | `test_operations_api_and_screens.py::test_navigation_points_only_at_live_screens` | 1.4 | | Done |
| INV-146 | Goods stay on the source branch's books until received | in-transit warehouse of the source branch | `test_transfers.py::test_a_cross_branch_dispatch_stays_on_the_source_branch_books` | 1.5 | | Done |
| INV-147 | A user can never select the in-transit warehouse | `_validate_transfer_endpoints` + trigger | `test_transfers.py::test_the_in_transit_warehouse_cannot_be_chosen`, `test_transfers.py::test_the_database_refuses_a_raw_in_transit_endpoint` | 1.5 | | Done |
| INV-148 | Cross-organization transfer is refused | `_validate_transfer_endpoints` | `test_transfers.py::test_a_cross_organization_transfer_is_refused` | 1.5 | | Done |
| INV-149 | Dispatch carries the exact outbound value into transit | `_post_dispatch_effects` | `test_transfers.py::test_a_full_depletion_carries_its_entire_remaining_value` | 1.5 | | Done |
| INV-150 | A receipt is valued from its own transfer, not the pooled average | `allocate`, `outbound_value` | `test_transfers.py::test_the_pooled_in_transit_average_is_not_used` | 1.5 | | Done |
| INV-151 | The final receipt or closure takes the exact remainder | `allocate` | `test_transfers.py::test_the_final_receipt_takes_the_exact_remaining_value` | 1.5 | | Done |
| INV-152 | Receipts plus shortage always equal the dispatch | `allocate` | `test_transfers.py::test_receipts_plus_shortage_equal_the_dispatch` | 1.5 | | Done |
| INV-153 | Over-receipt is refused against the locked remaining basis | `_allocate_receipt` | `test_transfers.py::test_over_receipt_is_refused` | 1.5 | | Done |
| INV-154 | Same-branch receipt posts one branch-local journal | `_post_receipt_journals` | `test_transfers.py::test_the_same_branch_receipt_journal_is_one_branch_local_entry` | 1.5 | AT-002 | Done |
| INV-155 | Cross-branch receipt posts two journals, each branch balanced | `_post_receipt_journals` | `test_transfers.py::test_a_cross_branch_receipt_writes_two_balanced_journals` | 1.5 | AT-002 | Done |
| INV-156 | Inter-branch clearing nets to zero for a complete event | ADR-020 §9 | `test_transfers.py::test_inter_branch_clearing_nets_to_zero` | 1.5 | | Done |
| INV-157 | Each side of a receipt is dated by its own branch's business day | `post_receipt` | `test_transfers.py::test_the_two_branches_may_resolve_to_different_dates` | 1.5 | | Done |
| INV-158 | Either branch's closed period rolls the whole receipt back | `_period_for` | `test_transfers.py::test_a_closed_source_period_refuses_the_whole_receipt`, `test_transfers.py::test_a_closed_destination_period_refuses_the_whole_receipt` | 1.5 | | Done |
| INV-159 | An unmapped role rolls back every stock and document effect | `_resolve_receipt_accounts` | `test_transfers.py::test_an_unmapped_clearing_role_rolls_everything_back` | 1.5 | | Done |
| INV-160 | A shortage closure needs permission, reason and cost centre | `create_shortage`, model constraints | `test_transfers.py::TestShortage` | 1.5 | AT-008 | Done |
| INV-161 | A storekeeper cannot close a shortage | `CLOSE_TRANSFER_SHORTAGE` role map | `test_transfers.py::test_a_storekeeper_cannot_close_a_shortage` | 1.5 | AT-008 | Done |
| INV-162 | At most one active closure per transfer | partial unique index | `test_transfers.py::test_only_one_closure_can_be_active`, `test_transfer_concurrency.py::test_two_closures_leave_at_most_one_active` | 1.5 | | Done |
| INV-163 | Reversing a receipt restores in-transit exactly | `reverse_receipt` | `test_transfers.py::test_reversing_a_receipt_restores_transit_exactly` | 1.5 | | Done |
| INV-164 | Consumed destination stock blocks a receipt reversal | `reverse_stock_entry` availability | `test_transfers.py::test_consumed_destination_stock_blocks_a_receipt_reversal` | 1.5 | | Done |
| INV-165 | Dispatch reversal is refused while any child is active | `reverse_dispatch` | `test_transfers.py::test_a_dispatch_cannot_be_reversed_while_a_receipt_stands` | 1.5 | | Done |
| INV-166 | The transfer's status is computed from its posted children | `recompute_transfer_status` | `test_transfers.py::TestReceipt`, `test_transfers.py::TestReversal` | 1.5 | | Done |
| INV-167 | The dispatch conversion snapshot is authoritative at receipt | `add_receipt_line` | `test_transfers.py::test_the_dispatch_conversion_snapshot_survives_a_new_factor` | 1.5 | | Done |
| INV-168 | A lot survives the journey unchanged | valuation key | `test_transfers.py::test_the_lot_survives_the_journey` | 1.5 | | Done |
| INV-169 | Each side of a receipt carries its own source identity | `TRANSFER_RECEIPT_{SOURCE,DESTINATION}_TYPE` | `test_transfers.py::test_the_source_identities_are_distinct_per_side` | 1.5 | AT-009 | Done |
| INV-170 | Posted transfers, receipts and closures are database-immutable | triggers, `inventory/0013` | `test_transfers.py::TestIdentityAndImmutability` | 1.5 | | Done |
| INV-171 | A journalled posting names an account for every dinar it moved | deferred constraint trigger | `test_transfers.py::test_a_journalled_posting_names_an_account_for_every_dinar` | 1.5 | | Done |
| INV-172 | The bare kernel still posts without any control account | conditional invariant | `test_transfers.py::test_a_bare_kernel_posting_still_needs_no_account` | 1.5 | | Done |
| INV-173 | Stock keys are locked canonically across a whole event | `acquire_stock_key_locks` | `test_transfer_concurrency.py::test_two_cross_branch_receipts_do_not_deadlock` | 1.5 | | Done |
| INV-174 | Concurrent receipts never resolve more than was dispatched | line row lock + deferred trigger | `test_transfer_concurrency.py::test_two_receipts_never_exceed_the_dispatch` | 1.5 | | Done |
| INV-175 | Transfer subledger and in-transit ledger both reconcile | `verify_transfer`, `verify_in_transit` | `test_transfers.py::TestReads` | 1.5 | AT-011 | Done |
| INV-176 | A receipt id under another transfer's route is a 404 | `resolve_receipt(transfer=…)` | `test_transfers.py::test_a_receipt_id_under_another_transfer_is_a_404` | 1.5 | AT-008 | Done |
| INV-177 | Transfer cost fields are omitted without view_valuation | `_serialize_transfer` | `test_transfer_api_and_screens.py::test_cost_is_omitted_without_view_valuation` | 1.5 | AT-008 | Done |
| INV-178 | A reason code's code and application are immutable once created | `inventory_reason_code_identity_is_immutable` | `test_waste_counts_adjustments.py::test_the_code_and_its_application_are_immutable_at_the_database` | 1.6 | | Done |
| INV-179 | An archived reason code stays reserved forever | unique per organization, never deleted | `test_waste_counts_adjustments.py::test_an_archived_code_stays_reserved` | 1.6 | | Done |
| INV-180 | A waste line names a reason code of the right application | `_validate_line_reason_code` + trigger | `test_waste_counts_adjustments.py::test_a_count_reason_cannot_be_used_on_a_waste_line` | 1.6 | | Done |
| INV-181 | Waste leaves at the current average, full depletion exact at zero | `_plan_waste` | `test_waste_counts_adjustments.py::test_the_last_waste_out_takes_the_entire_remaining_value` | 1.6 | AT-002 | Done |
| INV-182 | Waste requires the cost centre its class-6 account demands | `require_cost_center_where_the_account_demands_one` | `test_waste_counts_adjustments.py::test_waste_needs_a_cost_centre_because_its_account_demands_one` | 1.6 | AT-005 | Done |
| INV-183 | Expired lots leave only through waste, count loss or adjustment | `EXPIRED_LOT_REMOVAL_TYPES` | `test_waste_counts_adjustments.py::test_an_expired_lot_may_be_wasted_but_never_issued` | 1.6 | | Done |
| INV-184 | A warehouse is frozen iff `frozen_by_count` names an active count | `inventory_warehouse_freeze_owner_is_active` | `test_waste_counts_adjustments.py::test_a_frozen_warehouse_names_an_active_count_at_the_database` | 1.6 | | Done |
| INV-185 | A count may not finish while it still holds a warehouse frozen | `inventory_count_releases_its_freeze` | `test_count_concurrency.py::TestApprovalRaces` | 1.6 | | Done |
| INV-186 | A frozen warehouse refuses every posting | `_require_warehouses_are_not_frozen` | `test_waste_counts_adjustments.py::test_a_frozen_warehouse_refuses_every_posting` | 1.6 | | Done |
| INV-187 | At most one active count per warehouse | `stock_count_one_active_per_warehouse` | `test_count_concurrency.py::test_two_counts_race_and_exactly_one_starts` | 1.6 | | Done |
| INV-188 | The in-transit warehouse can never be counted | `_require_warehouse_is_countable` + check constraint | `test_waste_counts_adjustments.py::test_the_in_transit_warehouse_cannot_be_counted` | 1.6 | | Done |
| INV-189 | A posting and a freeze can never interleave | `lock_warehouses_shared` / `_exclusive` | `test_count_concurrency.py::test_an_issue_either_lands_in_the_snapshot_or_is_refused` | 1.6 | | Done |
| INV-190 | No transfer receipt lands outside the count snapshot | warehouse freeze lock | `test_count_concurrency.py::test_no_transfer_receipt_lands_outside_the_snapshot` | 1.6 | | Done |
| INV-191 | The book snapshot is immutable from the cutoff | `inventory_stock_count_line_follows_count` | `test_waste_counts_adjustments.py::test_the_book_snapshot_is_immutable` | 1.6 | | Done |
| INV-192 | The cutoff and its business-date snapshot are frozen at start | `inventory_stock_count_is_immutable` | `test_waste_counts_adjustments.py::test_the_cutoff_is_immutable` | 1.6 | AT-004 | Done |
| INV-193 | Counted figures freeze at submission | line trigger + status check | `test_waste_counts_adjustments.py::test_submission_computes_the_variance_and_freezes_the_figures` | 1.6 | | Done |
| INV-194 | The counting sheet carries no book quantity, in JSON or HTML | `blind_lines` | `test_count_api_and_screens.py::TestBlindCountEndpoint` | 1.6 | AT-008 | Done |
| INV-195 | The sheet stays blind for a caller holding view_valuation | `blind_count_sheet` | `test_count_api_and_screens.py::test_the_sheet_is_blind_even_with_view_valuation` | 1.6 | AT-008 | Done |
| INV-196 | The approver is never the conductor | service, API and check constraint | `test_waste_counts_adjustments.py::test_maker_checker_is_a_database_constraint_too` | 1.6 | AT-003 | Done |
| INV-197 | A direct POST cannot bypass maker-checker | `approve_count` | `test_count_api_and_screens.py::test_the_conductor_cannot_approve_their_own_count_by_direct_post` | 1.6 | AT-008 | Done |
| INV-198 | The book position at approval must equal the snapshot | `_require_snapshot_still_matches` | `test_waste_counts_adjustments.py::test_a_changed_book_position_refuses_to_post` | 1.6 | | Done |
| INV-199 | A gain into standing stock uses the standing average | `_resolve_variances` | `test_waste_counts_adjustments.py::test_a_gain_into_standing_stock_uses_the_standing_average` | 1.6 | AT-002 | Done |
| INV-200 | A zero-book gain requires an explicitly approved unit cost | `_resolve_variances` | `test_waste_counts_adjustments.py::test_a_zero_book_gain_needs_an_approved_unit_cost` | 1.6 | AT-002 | Done |
| INV-201 | An omitted cost and a confirmed zero are different answers | `_apply_approved_costs` | `test_waste_counts_adjustments.py::test_an_omitted_cost_and_a_confirmed_zero_are_different_answers` | 1.6 | | Done |
| INV-202 | A count loss takes the exact remaining value at zero | kernel full-depletion rule | `test_waste_counts_adjustments.py::test_a_full_loss_takes_the_exact_remaining_value` | 1.6 | AT-002 | Done |
| INV-203 | A missing mapping rolls back stock, journal, status and freeze | one transaction | `test_waste_counts_adjustments.py::test_a_missing_mapping_rolls_back_stock_journal_status_and_freeze` | 1.6 | AT-011 | Done |
| INV-204 | Cancelling unfreezes and keeps the whole history | `cancel_count` | `test_waste_counts_adjustments.py::test_cancelling_unfreezes_and_keeps_the_history` | 1.6 | | Done |
| INV-205 | A posted count reverses exactly and does not re-freeze | `reverse_count` | `test_waste_counts_adjustments.py::test_a_posted_count_reverses_exactly_and_does_not_refreeze` | 1.6 | | Done |
| INV-206 | Reversing a gain whose stock was consumed is refused | availability in `reverse_stock_entry` | `test_waste_counts_adjustments.py::test_reversing_a_gain_whose_stock_was_consumed_is_refused` | 1.6 | | Done |
| INV-207 | An active count blocks soft-close and close of its period | `refuse_close_while_a_count_is_active` | `test_waste_counts_adjustments.py::test_an_active_count_blocks_closing_its_period` | 1.6 | AT-004 | Done |
| INV-208 | A period close and a count start cannot both commit | period row lock | `test_count_concurrency.py::TestPeriodCloseRace` | 1.6 | | Done |
| INV-209 | A signless movement type must state its direction | `_validate_direction` | `test_ledger.py` + adjustment tests | 1.6 | | Done |
| INV-210 | A quantity gain needs an explicit unit cost | `_validate_gain_cost` | `test_waste_counts_adjustments.py::test_a_quantity_gain_needs_an_explicit_unit_cost` | 1.6 | | Done |
| INV-211 | A value-only revaluation moves no quantity | `apply_value_only` | `test_waste_counts_adjustments.py::test_a_value_only_write_up_moves_no_quantity` | 1.6 | AT-002 | Done |
| INV-212 | A revaluation against zero quantity is refused | `_require_position_can_be_revalued` | `test_waste_counts_adjustments.py::test_a_value_only_line_against_no_quantity_is_refused` | 1.6 | | Done |
| INV-213 | A revaluation cannot drive inventory value below zero | `_require_revaluation_stays_positive` | `test_waste_counts_adjustments.py::test_a_write_down_below_zero_is_refused` | 1.6 | | Done |
| INV-214 | Posted adjustments and their lines are immutable | `inventory_adjustment_is_immutable` | `test_waste_counts_adjustments.py::test_a_posted_adjustment_is_immutable_at_the_database` | 1.6 | | Done |
| INV-215 | Two adjustments on one key serialise deterministically | canonical stock-key locks | `test_count_concurrency.py::TestAdjustmentRaces` | 1.6 | | Done |
| INV-216 | Opposite-order multi-item postings never deadlock | canonical stock-key locks | `test_count_concurrency.py::test_opposite_order_multi_item_postings_do_not_deadlock` | 1.6 | | Done |
| INV-217 | Waste, count and adjustment all reconcile to their own effects | `verify_stock_count`, `verify_adjustment` | `test_waste_counts_adjustments.py` + `verify_inventory_accounting` | 1.6 | AT-011 | Done |
| INV-218 | Every frozen warehouse has exactly one active owning count | `verify_warehouse_freezes` | `test_waste_counts_adjustments.py::TestCountStartAndFreeze` | 1.6 | AT-011 | Done |
| INV-219 | A count line under another count's route is a 404 | `resolve_count_line(count=…)` | `test_count_api_and_screens.py::test_a_line_from_another_count_is_a_404_on_this_route` | 1.6 | AT-008 | Done |
| INV-220 | Cost fields are omitted, not blanked, without view_valuation | `exclude_unset=True` + conditional payload | `test_count_api_and_screens.py::test_a_storekeeper_sees_no_cost_on_the_review` | 1.6 | AT-008 | Done |
| INV-221 | Demo data never runs outside DEBUG | `seed_inventory_demo.handle` | `test_demo_seed.py::test_the_command_refuses_to_run_outside_debug` | 1.6a | | Done |
| INV-222 | Posting demo data needs an explicit --confirm-demo | `seed_inventory_demo.handle` | `test_demo_seed.py::test_without_confirm_demo_master_data_is_seeded_but_nothing_posts` | 1.6a | | Done |
| INV-223 | An ambiguous selector fails with the valid choices listed | `resolve_user`, `ensure_organization` | `test_demo_seed.py::test_an_ambiguous_user_is_refused_rather_than_guessed` | 1.6a | | Done |
| INV-224 | A second demo run creates no duplicate document, movement or journal | evidence-reference lookup per step | `test_demo_seed.py::test_a_second_run_creates_nothing` | 1.6a | | Done |
| INV-225 | Demo balances come from the valuation kernel, not from the seed | every step calls a domain service | `test_demo_seed.py::test_the_planned_balances_are_what_the_kernel_computed` | 1.6a | | Done |
| INV-226 | Demo data reconciles to the general ledger | `verify_inventory_accounting` | `test_demo_seed.py::test_reconciliation_is_clean` | 1.6a | AT-011 | Done |
| INV-227 | A demo reset never deletes posted stock or accounting history | `reset_demo` | `test_demo_seed.py::test_reset_removes_drafts_and_keeps_posted_history` | 1.6a | | Done |
| INV-228 | A demo reset archives reason codes rather than deleting them | `reset_demo` | `test_demo_seed.py::test_reset_archives_reason_codes_rather_than_deleting_them` | 1.6a | | Done |
| INV-229 | Every implemented inventory section renders seeded data | the demo scenario | `test_demo_seed.py::test_no_implemented_section_renders_empty` | 1.6a | | Done |
| INV-230 | An HX-Request returns the results partial, never a second page shell | `InventoryListView.is_htmx` | `test_list_htmx.py::test_an_hx_request_returns_only_the_results` | 1.6a | | Done |
| INV-231 | Authorization is identical on the partial and the full page | shared mixin and selector | `test_list_htmx.py::test_a_user_without_the_permission_is_refused_on_both_paths` | 1.6a | AT-008 | Done |
| INV-232 | Valuation redaction survives the htmx swap | shared `show_cost` context | `test_list_htmx.py::test_a_caller_without_it_gets_no_cost_column_in_the_partial` | 1.6a | AT-008 | Done |
| INV-233 | Paging keeps every filter, not just the search term | `InventoryListView.filter_query` | `test_list_htmx.py::test_paging_keeps_the_filter` | 1.6a | | Done |
| INV-234 | htmx is the vendored pinned version and is loaded exactly once | `base.html` | `test_list_htmx.py::test_the_vendored_file_is_the_pinned_version` | 1.6a | | Done |
| INV-235 | Expired stock may be received but never issued | `EXPIRED_LOT_REMOVAL_TYPES` | `test_demo_seed.py::test_the_expired_lot_was_received_and_written_off_to_zero` | 1.6a | | Done |
| INV-236 | Waste clears an expired lot to zero quantity and zero value | full-depletion rule | `test_demo_seed.py::test_the_expired_lot_was_received_and_written_off_to_zero` | 1.6a | AT-002 | Done |
| INV-237 | All three adjustment kinds are exercised end to end | `post_adjustment` | `test_demo_seed.py::test_all_three_adjustment_kinds_are_represented` | 1.6a | | Done |
| INV-238 | A value-only adjustment moves value and not quantity | `apply_value_only` | `test_demo_seed.py::test_the_value_only_adjustment_moved_value_and_not_quantity` | 1.6a | AT-002 | Done |
| INV-239 | All four count states are visible at once | the demo scenario | `test_demo_seed.py::test_the_whole_count_lifecycle_is_visible_at_once` | 1.6a | | Done |
| INV-240 | A cancelled count releases its freeze and is kept | `cancel_count` | `test_demo_seed.py::test_a_cancelled_count_releases_its_freeze_and_is_kept` | 1.6a | | Done |
| INV-241 | A submitted count still holds its warehouse freeze | `ACTIVE_COUNT_STATUSES` | `test_demo_seed.py::test_a_submitted_count_still_holds_its_warehouse_freeze` | 1.6a | | Done |
| INV-242 | The pagination carry is shared by every list family | `apps.core.context_processors._filter_query` | `apps/core/tests/test_list_filter_query.py` | 1.6a | | Done |
| INV-243 | A report never trusts a submitted organization/branch/warehouse id | `reports.scoped_warehouses` narrows `readable_warehouses` | `test_reports_and_exports.py::test_a_submitted_organization_id_cannot_widen_scope` | 1.7A | AT-008 | Done |
| INV-244 | A global permission without reach grants no report rows | ADR-016 scope selectors | `test_reports_and_exports.py::test_a_global_permission_without_reach_grants_nothing` | 1.7A | AT-008 | Done |
| INV-245 | POSTED_AS_OF uses the kernel's stored running totals | `_positions_from` prefix branch | `test_reports_and_exports.py::test_posted_as_of_uses_the_kernels_running_totals` | 1.7A | | Done |
| INV-246 | EFFECTIVE_DATE sums deltas and derives the average | `_positions_from` non-prefix branch | `test_reports_and_exports.py::test_effective_date_sums_deltas_and_derives_the_average` | 1.7A | | Done |
| INV-247 | The two historical modes may legitimately disagree | two cutoffs over one movement set | `test_reports_and_exports.py::test_the_two_modes_can_return_different_answers_for_one_window` | 1.7A | | Done |
| INV-248 | Every historical report states which mode produced it | `_base_report.html` mode row | `test_reports_and_exports.py::test_the_mode_is_shown_on_every_historical_screen` | 1.7A | | Done |
| INV-249 | Screen and export share one scoped service and one filter set | `InventoryReportView.report_rows` | `test_reports_and_exports.py::test_export_rows_match_the_screen` | 1.7A | | Done |
| INV-250 | Valuation is omitted, never blanked, in HTML, partial and CSV | `include_valuation` row builders | `test_reports_and_exports.py::test_a_caller_without_valuation_sees_no_cost_heading`, `test_reports_and_exports.py::test_export_omits_cost_columns_without_permission` | 1.7A | AT-008 | Done |
| INV-251 | Exported cells never pass through float | `neutralise` formats Decimal | `test_reports_and_exports.py::test_decimals_never_pass_through_float` | 1.7A | | Done |
| INV-252 | Exported cells cannot execute in a spreadsheet | `FORMULA_TRIGGERS` prefixing | `test_reports_and_exports.py::test_formula_triggers_are_neutralised` | 1.7A | | Done |
| INV-253 | Export filenames cannot traverse or disguise | `safe_filename` | `test_reports_and_exports.py::test_the_filename_is_safe_and_dated` | 1.7A | | Done |
| INV-254 | GL reconciliation reuses the authoritative verifier | `_control_account_of` + `verify_inventory_against_gl` | `test_reports_and_exports.py::test_gl_reconciliation_agrees_and_offers_no_repair` | 1.7A | AT-011 | Done |
| INV-255 | Planted GL drift stays visible and is never repaired | no repair path exists | `test_reports_and_exports.py::test_a_manual_journal_against_control_shows_as_drift` | 1.7A | AT-011 | Done |
| INV-256 | Import upload and preview mutate no master data | `validate_batch` writes only verdicts | `test_imports_and_projection.py::test_upload_and_validate_change_no_master_data` | 1.7A | | Done |
| INV-257 | One invalid row prevents the whole apply | `apply_batch` refuses on `error_row_count` | `test_imports_and_projection.py::test_one_invalid_row_stops_the_whole_batch` | 1.7A | AT-012 | Done |
| INV-258 | Import apply writes through the approved master-data services | `WRITERS` call `set_branch_item_setting` etc. | `test_imports_and_projection.py::test_apply_writes_and_records_what_changed` | 1.7A | | Done |
| INV-259 | Re-applying the same content is a clean domain conflict | partial unique index + pre-check | `test_imports_and_projection.py::test_the_same_content_under_a_new_batch_is_refused_cleanly` | 1.7A | | Done |
| INV-260 | The file fingerprint ignores column order and quoting | `fingerprint` sorts keys | `test_imports_and_projection.py::test_the_fingerprint_ignores_column_order_and_quoting` | 1.7A | | Done |
| INV-261 | A file naming one record twice is refused entirely | duplicate-key poisoning in `validate_batch` | `test_imports_and_projection.py::test_a_file_naming_the_same_record_twice_is_refused` | 1.7A | | Done |
| INV-262 | Macro workbooks, oversize, empty, malformed and duplicate-header files are refused | `parse_rows` guards | `test_imports_and_projection.py::TestUploadSecurity` | 1.7A | AT-008 | Done |
| INV-263 | Stored upload filenames cannot traverse or carry bidi overrides | `sanitise_filename` | `test_imports_and_projection.py::test_the_stored_filename_cannot_traverse_or_disguise` | 1.7A | AT-008 | Done |
| INV-264 | An import row cannot reference another organization's master data | validators resolve within the batch organization | `test_imports_and_projection.py::test_a_foreign_branch_is_refused` | 1.7A | AT-008 | Done |
| INV-265 | An import kind with no writer cannot be uploaded | `VALIDATORS` gate in `create_batch` | `test_imports_and_projection.py::test_an_unsupported_kind_cannot_be_uploaded` | 1.7A | | Done |
| INV-266 | Viewing import history does not imply applying | separate permissions | `test_imports_and_projection.py::test_a_direct_post_without_the_kind_permission_is_refused` | 1.7A | AT-008 | Done |
| INV-267 | The projection replays to the ledger on every compared field | `verify_organization` | `test_imports_and_projection.py::test_a_clean_projection_verifies` | 1.7A | AT-007 | Done |
| INV-268 | Planted quantity, value, average, sequence and control drift are all detected | `verify_organization` field comparisons | `test_imports_and_projection.py::test_planted_drift_is_detected`, `test_imports_and_projection.py::test_planted_control_account_drift_is_detected` | 1.7A | AT-007 | Done |
| INV-269 | Projection verification mutates nothing and offers no repair | read-only by construction | `test_imports_and_projection.py::test_verification_mutates_nothing`, `test_imports_and_projection.py::test_there_is_no_repair_mode` | 1.7A | AT-007 | Done |
| INV-270 | A depleted position replays to exactly zero and drops its control account | full-depletion rule mirrored in the replay | `test_imports_and_projection.py::test_a_fully_depleted_position_replays_to_exactly_zero` | 1.7A | AT-007 | Done |
| INV-271 | An unknown verification selector exits 2 rather than reporting clean | `_resolve_scope` | `test_imports_and_projection.py::test_an_unknown_selector_exits_two` | 1.7A | | Done |
| INV-272 | Import batch row counts, branch/kind pairing and applied state hold at COMMIT | migration 0016 constraints | `test_imports_and_projection.py::TestImportConstraints` | 1.7A | | Done |
| INV-273 | A location carries quantity and never value | schema has no money columns | `test_locations.py::test_the_balance_model_has_no_money_columns` | 1.7B | | Done |
| INV-274 | A move between bins posts no stock movement and does not revalue | `move_between_locations` | `test_locations.py::test_a_move_between_bins_posts_no_stock_movement` | 1.7B | | Done |
| INV-275 | A put-away cannot exceed the unlocated remainder | `put_away` under the position lock | `test_locations.py::test_putting_away_more_than_is_unlocated_is_refused` | 1.7B | | Done |
| INV-276 | An issue naming no bin still leaves the invariant true | `release_for_outbound`, called by the ledger | `test_locations.py::test_an_issue_that_names_no_bin_still_leaves_the_invariant_true` | 1.7B | AT-007 | Done |
| INV-277 | Two concurrent put-aways cannot both take the same unlocated stock | `(warehouse, item, lot)` advisory lock | `test_locations.py::TestLocationConcurrency` | 1.7B | | Done |
| INV-278 | Bins claiming more than the warehouse holds is detected | `verify_locations` | `test_locations.py::test_planted_over_allocation_is_detected` | 1.7B | AT-007 | Done |
| INV-279 | A system warehouse takes no locations | `create_location` | `test_locations.py::test_a_system_warehouse_takes_no_locations` | 1.7B | | Done |
| INV-280 | A location holding stock cannot be archived | `update_location` | `test_locations.py::test_a_location_holding_stock_cannot_be_archived` | 1.7B | | Done |

## Phase 2 — Procurement and Accounts Payable

Established by `docs/tasks/task-2-0-procurement-domain-spec.md` (2026-08-11).
Every row is `Specified` until its task lands: Task 2.0 wrote no code, and a
row that claimed evidence today would be claiming a test that does not exist —
which is the failure `tests/test_traceability.py` was written to stop.

Requirement identifiers are **repository-local**. No SRS exists to map them to;
see Task 2.0 §0.

| ID | Requirement | Implementation | Test | Task | AT | Status |
|---|---|---|---|---|---|---|
| PRC-001 | No model combines two of the seven procurement events | separate aggregates | — | 2.1–2.15 | | Specified |
| PRC-002 | Supplier code canonical uppercase, unique per organization, archived codes reserved | `create_supplier` + `UniqueConstraint` | — | 2.1 | | Specified |
| PRC-003 | `Supplier` carries no balance field; balances derive from posted documents | model shape | — | 2.1 | | Specified |
| PRC-004 | A supplier is archived, never deleted | `on_delete=PROTECT` | — | 2.1 | | Specified |
| PRC-005 | A catalogue price values nothing; no posting service reads it | AST boundary test | — | 2.2 | | Specified |
| PRC-006 | One preferred supplier per item; one preferred catalogue row per pair | partial unique index | — | 2.2 | | Specified |
| PRC-007 | Catalogue effective periods cannot overlap | `EXCLUDE USING gist` | — | 2.2 | | Specified |
| PRC-008 | A catalogue package must be one the item can convert to base | service guard | — | 2.2 | | Specified |
| PRC-009 | A purchase request has no stock and no accounting effect | asserted per status | — | 2.3 | | Specified |
| PRC-010 | A request approver is never its submitter | `CheckConstraint` | — | 2.3 | | Specified |
| PRC-011 | Only a DRAFT request is editable | service guard + trigger | — | 2.3 | | Specified |
| PRC-012 | Request lines snapshot conversion, version, factor and base quantity | non-null columns | — | 2.3 | | Specified |
| PRC-013 | A quotation has no stock and no accounting effect | asserted per status | — | 2.4 | | Specified |
| PRC-014 | Comparison normalises to base quantity and base unit price | comparison service | — | 2.5 | | Specified |
| PRC-015 | Freight is shown separately **and** inside a landed unit price | comparison report | — | 2.5 | | Specified |
| PRC-016 | No automatic lowest-price award; a human names a reason | no auto-select path exists | — | 2.5 | | Specified |
| PRC-017 | An award records actor, reason, and the same-organization check | `award_quotation` | — | 2.5 | | Specified |
| PRC-018 | A purchase order creates no stock and no payable, including ISSUED | asserted per status | — | 2.6 | | Specified |
| PRC-019 | Issued terms are immutable; a change creates a version | allowlist trigger + version model | — | 2.7 | | Specified |
| PRC-020 | A revision cannot reduce quantity below what was received | service guard under a lock | — | 2.7 | | Specified |
| PRC-021 | The supplier cannot change once a receipt exists | service guard | — | 2.7 | | Specified |
| PRC-022 | Cancellation needs a reason, is refused after a receipt, and is terminal | service guard | — | 2.7 | | Specified |
| PRC-023 | Over-receipt is refused at zero tolerance | service guard under a lock | — | 2.8 | | Specified |
| PRC-024 | Delivered equals accepted plus rejected on every line | `CheckConstraint` | — | 2.8 | | Specified |
| PRC-025 | Only accepted quantity increases stock | posting service | — | 2.8 | | Specified |
| PRC-026 | A VARIABLE package line requires its measured quantity | reuses the inventory guard | — | 2.8 | | Specified |
| PRC-027 | Lot and expiry follow the rules of the item, unchanged | reuses `_validate_lot` | — | 2.8 | | Specified |
| PRC-028 | A receipt without a purchase order is permitted, but never without a price | service guard | — | 2.8 | | Specified |
| PRC-029 | Receipts post through the inventory kernel; no second posting path | AST boundary test | — | 2.8 | | Specified |
| PRC-030 | Partial receipt; cumulative accepted tracked against the order line | selector + guard | — | 2.8 | | Specified |
| PRC-031 | A posted receipt is immutable; correction is reversal plus replacement | allowlist trigger | — | 2.8 | | Specified |
| PRC-032 | Receipt journal value equals receipt stock value, per line, to 3 dp | reconciliation test | — | 2.9 | | Specified |
| PRC-033 | Grouped debits where items resolve to different control accounts | reuses the opening-stock shape | — | 2.9 | | Specified |
| PRC-034 | No account, id or code is named in a posting service | effective-dated role resolution | — | 2.9 | | Specified |
| PRC-035 | Source identity is complete or absent, never partial | reuses the accounting guard | — | 2.9 | | Specified |
| PRC-036 | Document, movement, journal and status commit or roll back together | `transaction.atomic()` | — | 2.9 | | Specified |
| PRC-037 | Supplier invoice number unique per supplier over non-reversed invoices | partial unique index | — | 2.10 | | Specified |
| PRC-038 | A supplier invoice never mutates stock | asserted: no movements | — | 2.10 | | Specified |
| PRC-039 | An invoice total is the sum of its lines; freight allocated, never rated | `apps/core/allocation.py` | — | 2.10 | | Specified |
| PRC-040 | Allocation is many-to-many and partial | `MatchAllocation` | — | 2.11 | | Specified |
| PRC-041 | Over-allocation is impossible on both sides | service under a lock + verifier | — | 2.11 | | Specified |
| PRC-042 | Matching status is derived, never a stored mutable flag | selector only | — | 2.11 | | Specified |
| PRC-043 | Price variance never restates a posted movement or a closed period | posting service | — | 2.12 | | Specified |
| PRC-044 | The on-hand versus consumed split is deterministic | `apps/core/allocation.py` | — | 2.12 | | Specified |
| PRC-045 | Release 1 expenses the variance; revaluation is an explicit permissioned act | ADR-022 | — | 2.12 | | Specified |
| PRC-046 | Landed-cost capitalisation is captured but not implemented | fields present, no posting | — | 2.12 | | Specified |
| PRC-047 | A supplier return is not an inventory `RETURN_IN` | distinct movement type | — | 2.13 | | Specified |
| PRC-048 | A return leaves stock at the standing moving average | reuses the kernel; ADR-022 | — | 2.13 | | Specified |
| PRC-049 | The average-versus-credit difference is a purchase return variance | posting service | — | 2.13 | | Specified |
| PRC-050 | Negative stock is refused on a return, with no bypass | reuses `_require_available` | — | 2.13 | | Specified |
| PRC-051 | A credit note reduces the payable or stands as unallocated credit; no stock | posting service | — | 2.14 | | Specified |
| PRC-052 | Credit note document number unique per supplier over non-reversed notes | partial unique index | — | 2.14 | | Specified |
| PRC-053 | Partial payment across several invoices is normal | `PaymentAllocation` | — | 2.15 | | Specified |
| PRC-054 | Payment over-allocation is impossible on both sides | service under a lock + verifier | — | 2.15 | | Specified |
| PRC-055 | An unallocated remainder is a supplier advance, never a negative payable | posting service | — | 2.15 | | Specified |
| PRC-056 | Cash and bank come from effective-dated roles, never an id | two new `AccountRole` rows | — | 2.15 | | Specified |
| PRC-057 | Oldest-invoice allocation is a visible default, never silent | UI default; API requires explicit | — | 2.15 | | Specified |
| PRC-058 | Procurement-to-GL reconciliation proves four equalities | `verify_procurement_accounting` | — | 2.16 | | Specified |
| PRC-059 | Verification reports and refuses to repair | no repair mode exists | — | 2.16 | | Specified |
| PRC-060 | Receipt and return permissions are warehouse-scoped; money is organization-scoped | `PERMISSION_SCOPE` | — | 2.1–2.15 | | Specified |
| PRC-061 | Cost columns are omitted, not blanked, without `view_supplier_cost` | view layer | — | 2.8 | | Specified |
| PRC-062 | No writable CRUD API and no writable admin for a posted record | command API + read-only admin | — | 2.17 | | Specified |
| PRC-063 | API money and quantities are exact strings in both directions | schema layer | — | 2.1–2.15 | | Specified |
| PRC-064 | Every command carries an organization-scoped idempotency key and fingerprint | reuses ADR-017 | — | 2.1–2.15 | | Specified |
| PRC-065 | Arabic RTL screens, logical properties, HTMX filters surviving pagination | templates + `_filter_query` | — | 2.1–2.16 | | Specified |
| PRC-066 | Demo data: three suppliers, the five existing items, idempotent, DEBUG-only | demo tooling | — | 2.1–2.17 | | Specified |
| PRC-067 | `source_document_id` is the immutable `public_id`, never a number or pk | posting services | — | 2.9 | | Specified |
