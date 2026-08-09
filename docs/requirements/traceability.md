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
| ORG-001 | Branch belongs to exactly one organization | organizations | `Branch.organization` (PROTECT) | `apps/organizations/tests/test_models.py::TestBranch` | Done | ADR-007 |
| ORG-002 | Branch codes are unique within an organization, not globally | organizations | `branch_code_unique_per_organization` | `::test_two_organizations_may_reuse_the_same_branch_code` | Done | |
| ORG-003 | Bilingual names on organization and branch | organizations | `name_ar`, `name_en` | `::test_names_are_stored_in_both_languages` | Done | Stored data, not translated strings |
| ORG-004 | Branch carries its own timezone and operating-day cutoff | organizations | `Branch.timezone`, `business_day_start_time` | `::test_unknown_timezones_are_rejected`, `::test_business_day_start_time_is_required` | Done | ADR-008; cutoff **value** still open |
| ORG-005 | A user may hold access to several branches | organizations | `BranchMembership` | `::test_a_user_may_hold_several_branches` | Done | Why User has no branch field |
| ORG-006 | One role per user per branch | organizations | `membership_unique_per_user_and_branch` | `::test_one_role_per_user_per_branch` | Done | |
| ORG-007 | A user cannot access an unassigned branch | organizations | `selectors.can_access_branch` | `apps/organizations/tests/test_selectors.py::TestAccessIsGranted` | Done | |
| ORG-008 | Cross-organization access is rejected | organizations | `selectors.accessible_branches` | `::test_member_does_not_see_another_organizations_branch` | Done | The isolation test |
| ORG-009 | Inactive user, branch, or organization removes access | organizations | `selectors.accessible_branches` | `::TestAccessIsWithdrawn` | Done | |
| ORG-010 | Revoking access preserves the record | organizations | `services.revoke_branch_access` | `::test_revoking_keeps_the_record` | Done | Deactivate, never delete |
| ORG-011 | Organizations and branches cannot be deleted while referenced | organizations | `on_delete=PROTECT` | `::test_organization_cannot_be_deleted_while_branches_exist` | Done | |
| ORG-012 | Superuser access is explicit and holds no implied role | organizations | `selectors.accessible_branches`, `role_at_branch` | `::TestSuperuser` | Done | |
| NAV-001 | Shell shows every module in the approved build order | core | `apps/core/navigation.py` | `apps/core/tests/test_shell.py::TestNavigationDefinition` | Done | |
| NAV-002 | Unbuilt sections are visible but inert | core | `templates/shell.html` | `::test_unbuilt_sections_are_inert` | Done | No links to 404s |
| NAV-003 | Any module's sidebar can be previewed before it is built | core | `context_processors.shell` | `::test_every_module_sidebar_can_be_previewed` | Done | `?module=` |
| NAV-004 | Unknown module parameter falls back safely | core | `context_processors.shell` | `::test_unknown_module_falls_back_instead_of_erroring` | Done | Allow-list of known keys |
| QTY-001 | Quantities store at 3 dp, ROUND_HALF_UP | core | `apps/core/quantity.py::quantize_quantity` | `apps/core/tests/test_quantity.py::TestRoundingDirection` | Done | ADR-006 |
| QTY-002 | Ties round away from zero, symmetrically | core | `QUANTITY_ROUNDING` | `::test_negative_ties_round_away_from_zero`, `::test_rounding_is_symmetric_in_magnitude` | Done | Required for reversals to cancel exactly |
| QTY-003 | Rounding happens once, never mid-calculation | core, units | `convert` vs `convert_to_stored_quantity` | `::TestNoDoubleRounding`, `::test_convert_does_not_round` | Done | 1.00049999 distinguishes the paths |
| QTY-004 | No float may enter a quantity path | core | `ensure_decimal` | `::TestFloatRejection` | Done | bool and non-finite rejected too |
| QTY-005 | Arabic-Indic numerals accepted; mixed scripts refused | core | `normalize_digits` | `::TestArabicNumerals` | Done | Matters for Phase 8 OCR ingestion |
| QTY-006 | Money cannot reuse quantity rounding | core | `apps/core/quantity.py` naming | — | Partial | Enforced by separation and naming; money module not yet written |
| UOM-001 | Every dimension has exactly one base unit | units | `unit_one_base_per_dimension` | `apps/units/tests/test_models.py::TestSeed`, `::TestDatabaseConstraints` | Done | Partial unique index |
| UOM-002 | Conversion factors are positive and base factor is 1 | units | `unit_factor_is_positive`, `unit_base_factor_is_one` | `::test_zero_factor_is_refused`, `::test_negative_factor_is_refused` | Done | DB-enforced |
| UOM-003 | Cross-dimension conversion is refused | units | `services._require_same_dimension` | `apps/units/tests/test_conversion.py::TestDimensionSafety` | Done | A kg of rice is not a litre |
| UOM-004 | Converting to base and back is lossless at 3 dp | units | `services.convert` | `::test_round_trip_is_lossless_within_the_declared_precision` | Done | Hypothesis property |
| UOM-005 | Golden cases match hand calculation | units | `services.convert_to_stored_quantity` | `::TestGoldenCases` | Done | `docs/testing/golden-cases/units-conversion.md` |
| UOM-006 | Packaging and yield are NOT unit conversions | units | seed list, module docstrings | `::test_packaging_units_are_deliberately_absent` | Done | Phase 1 and Phase 3 respectively |
| UOM-007 | Factors stored once to base; inverses derived | units | `factor_to_base` | `apps/units/tests/test_conversion.py::TestBaseHelpers` | Done | No independent reciprocals to disagree |
| UOM-008 | 12-dp factor precision stores an ounce exactly | units | `FACTOR_PLACES` | `::test_an_ounce_is_stored_to_full_precision` | Done | Confirmed, not to be reduced |
| MON-001 | Posted amounts store at 3 dp, ROUND_HALF_UP | core | `money.quantize_money` | `apps/core/tests/test_money.py::TestPostedAmountPrecision` | Done | ADR-012 |
| MON-002 | IQD displays at 0 dp; display values are never stored | core | `money.money_for_display` | `::TestDisplay` | Done | |
| MON-003 | Unit prices and rates keep 6 dp internally | core | `quantize_unit_price`, `quantize_rate` | `::TestHigherInternalPrecision` | Done | |
| MON-004 | Money shares no rounding with quantities | core | separate modules and naming | `::test_money_precision_is_independent_of_quantity_precision` | Done | |
| MON-005 | Reversals cancel exactly | core | `MONEY_ROUNDING` | `::test_rounding_is_symmetric_so_reversals_cancel` | Done | Ties away from zero |
| MON-006 | Allocated lines sum exactly to the source amount | core | `allocation.allocate_proportionally` | `apps/core/tests/test_allocation.py::test_parts_always_sum_to_the_whole` | Done | Largest remainder; Hypothesis property |
| MON-007 | Residual ties break on line order | core | `allocation` sort key | `::TestDeterminism` | Done | Caller must pass a stable order |
| MON-008 | A rate is applied to the total, not line by line | core | `allocation.allocate_by_rate` | `::test_rate_is_applied_to_the_total_not_line_by_line` | Done | |
| MON-009 | Credit notes mirror their invoice line for line | core | sign handling in `allocation` | `::test_reversal_is_the_exact_mirror` | Done | Hypothesis property |
| MON-010 | Nearest-250 rounding is off | core | `CASH_ROUNDING_ENABLED` | `::TestCashRoundingIsOff` | Done | Tripwire test |
| MON-011 | Cash rounding residual posts to an explicit account | core | `apply_cash_settlement_rounding` returns `(rounded, adjustment)` | `::test_the_adjustment_always_reconciles` | Partial | Account seeded in Task 0.6 (ADR-014) |
| MON-012 | Rendered money cannot enter arithmetic | core | `money_display` / `money_audit` / `money_export` return `str` | `::TestRendering` | Done | Structural, not conventional |
| MON-013 | Audit and export views expose the stored third decimal | core | `money_audit`, `money_export` | `::test_audit_views_expose_the_stored_third_decimal` | Done | |
| MON-014 | Reconciliation compares stored values, never displayed | core | renderers return `str` | `::test_reconciliation_must_compare_stored_values` | Done | |
| MON-015 | Allocation requires an explicit unique sequence | core | `AllocationItem.sequence`, `_validate_sequences` | `::TestSequenceValidation` | Done | Missing, duplicate, negative, non-integer all refused |
| MON-016 | Caller order never changes an allocation | core | sort by sequence before allocating | `::test_caller_order_does_not_change_the_outcome`, `::test_shuffling_the_input_never_changes_the_result` | Done | Hypothesis property |
| AUD-001 | Every audited action records actor, reason, and correlation | core | `record_audit_event` | `apps/core/tests/test_audit.py::TestRecording` | Done | Actor from context, not arguments |
| AUD-002 | The audit trail is append-only | core | PostgreSQL trigger, migration `core.0002` | `::TestImmutability` | Done | ORM, bulk update, and raw SQL all refused |
| AUD-003 | Events from one unit of work share a correlation id | core | `apps/core/context.py`, middleware | `::TestCorrelation` | Done | Echoed as `X-Correlation-ID` |
| AUD-004 | Audit snapshots preserve Decimal exactness | core | `services._json_safe` | `::test_decimals_are_stored_as_strings_not_floats` | Done | Decimals stored as strings, never floats |
| AUD-005 | Secrets are never captured in a snapshot | core | `NEVER_SNAPSHOT` | `::test_sensitive_fields_are_never_captured` | Done | |
| AUD-006 | Actor identity survives a later rename | core | `actor_label` denormalised | `::test_the_actor_name_is_kept_as_text` | Done | |
| AUD-007 | A user with audit events cannot be deleted | core | `on_delete=PROTECT` | `::test_an_actor_with_events_cannot_be_deleted` | Done | |
| AUD-008 | Mutable master data keeps row history | organizations, units, users | `HistoricalRecords` | `::TestRowHistory` | Done | Password excluded from user history |
| AUD-009 | Audit context never leaks between requests | core | middleware resets in `finally` | `::test_context_does_not_leak_between_requests` | Done | Reset even when the view raises |

## Task 0.7 — permissions, scope, API, idempotency

| ID | Requirement | App | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|---|
| PRM-001 | Twelve named accounting permissions exist | accounting | `Meta.permissions` on `JournalEntry`, `Account`, `CostCenter`, `AccountingPeriod` | `test_permissions.py::TestThePermissionsExist` | Done | Not Django add/change/delete |
| PRM-002 | Roles carry permissions through groups | organizations | `permissions.sync_user_role_groups`, `role:<ROLE>` groups | `::TestRoleGroupsFollowMemberships` | Done | Recomputed, never incremented |
| PRM-003 | ACCOUNTING_MANAGER holds `reopen_period` | accounting | `ROLE_PERMISSIONS` | `::test_accounting_manager_may_reopen` | Done | ADR-013 amendment |
| PRM-004 | Branch Manager, Branch Accountant, Cashier, warehouse roles do not | accounting | `ROLE_PERMISSIONS` | `::test_no_other_role_may_reopen` | Done | Parametrised over every excluded role |
| PRM-005 | Services check permission and scope, never a role name | accounting | `commands.py` | `test_security.py` | Done | No role string appears in a service |
| SCP-001 | Organization scope comes from `OrganizationMembership` only | organizations | `authorization.organization_scope` | `::test_branch_authority_is_never_organization_authority` | Done | Branch memberships never accumulate |
| SCP-002 | Organization authority reaches every branch in it | organizations | `selectors.accessible_branches` | `test_api.py::TestSoftClosedPeriodOverHttp` | Done | Containment is one-directional |
| SCP-003 | A submitted `organization_id` cannot widen access | organizations | `authorization.resolve_organization` | `::test_1_submitting_a_foreign_organization_id_is_refused` | Done | 403, not a silent filter |
| SCP-004 | A submitted `branch_id` cannot widen access | organizations | `authorization.resolve_branch` | `::test_2_submitting_a_foreign_branch_id_is_refused` | Done | Same organization, different branch |
| SCP-005 | A foreign account or cost centre cannot be injected | accounting | `commands._scoped_account`, `_scoped_cost_center` | `::TestForeignObjectInjection` | Done | Filtered, not fetched-then-checked |
| SCP-006 | Authority is needed at every branch an entry touches | accounting | `commands._require_at_every_branch` | `::test_an_entry_spanning_two_branches_needs_authority_at_both` | Done | Not "at least one" |
| SCP-007 | Period acts require organization scope | accounting | `PERMISSION_SCOPE` | `::test_9_a_branch_accountant_cannot_close_a_period` | Done | Holds the permission, holds it nowhere |
| API-001 | Commands, not writable CRUD, for posted ledger state | accounting | `apps/accounting/api.py` | `test_api.py` | Done | No PUT; PATCH is drafts only |
| API-002 | The API never reaches the kernel directly | accounting | import boundary | `::test_14c_the_api_layer_never_imports_the_kernel_directly` | Done | Architectural test over the AST |
| API-003 | Endpoints authenticate by default | config | `NinjaAPI(auth=django_auth)` | `::TestAuthenticationIsRequired` | Done | `/health` is the only exception |
| API-004 | Errors map to 403 / 404 / 409 / 422 | config | `config/api.py` exception handlers | `test_api.py` | Done | Conflict codes listed explicitly |
| API-005 | Money crosses the boundary as exact decimal strings | accounting | `LineIn` str fields, `money_export` | `::TestExactDecimalTransport` | Done | Checked against raw JSON, both directions |
| API-006 | API decimals are never grouped or localised | accounting | `money_export` | `::test_amounts_are_never_grouped_or_localised` | Done | Technical identity, not display |
| IDM-001 | One economic event, one journal, per organization | accounting | `journal_entry_source_event_unique_per_organization` | `::TestTheGuaranteeSurvivesACommit` | Done | Partial unique index, real COMMIT |
| IDM-002 | `source_event` is a closed enum | accounting | `SourceEvent` + `journal_entry_source_event_is_known` | `::TestTheEnumIsClosed` | Done | Typos refused by app and database |
| IDM-003 | A source identity is complete or absent | accounting | `validate_source_identity` + check constraint | `::TestIdentityIsCompleteOrAbsent` | Done | Manual journals carry none |
| IDM-004 | A retry returns the existing journal | accounting | `post_entry` idempotency key | `::test_a_retried_command_returns_the_same_journal` | Done | |
| IDM-005 | A conflicting reuse is a domain error, not an IntegrityError | accounting | `source_event_already_posted` | `::test_the_same_event_under_a_different_key_is_a_conflict` | Done | Names the entry that holds it |
| IDM-006 | The same source id is allowed in another organization | accounting | organization in the index | `::test_the_same_source_id_is_allowed_in_another_organization` | Done | |
| IDM-007 | POSTED and REVERSED coexist for one document | accounting | `reverse_entry` sets `SourceEvent.REVERSED` | `::TestPostedAndReversedCoexist` | Done | |
| IDM-008 | Source identity is immutable once posted | accounting | immutability trigger | `::TestSourceIdentityIsImmutable` | Done | |
| ADM-001 | Posted ledger state is read-only in the admin | accounting | `ReadOnlyAdminMixin` | `test_admin_lockdown.py` | Done | For superusers too |
| ADM-002 | The admin URLs refuse, not just the permission methods | accounting | Django admin | `::TestTheAdminUrlsRefuse` | Done | POST to change and delete both checked |
| LDG-001 | A posted entry is immutable on every column | accounting | migration `0005` allowlist trigger | `::test_no_other_posted_column_can_be_rewritten_either` | Done | **Fixes a Task 0.6 defect** |
| LDG-002 | A draft promoted to POSTED is balanced | accounting | trigger `accounting_journalentry_balance_on_post` | `::test_an_unbalanced_draft_is_refused_at_posting_not_at_creation` | Done | The 0002 trigger fires on lines only |
| LDG-003 | Drafts consume no journal number | accounting | partial unique + check constraint | `::test_create_amend_post` | Done | Numbering stays gapless |
| LDG-004 | Soft-closed posting needs authority and a reason | accounting | `_require_soft_close_override` | `::TestSoftClosedPeriodOverHttp` | Done | Override audited separately |
| LDG-005 | A reopening records actor, org, period, both states, reason | accounting | `reopen_accounting_period` | `::test_the_reopening_records_actor_organization_period_states_and_reason` | Done | |

## Task 0.8 — Phase 0 exit gate

| ID | Requirement | App | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|---|
| EXIT-001 | ACCOUNTANT holds no structural authority by default | accounting | `ROLE_PERMISSIONS` | `test_permissions.py::test_the_accountant_holds_no_structural_authority` | Done | Chart, cost centres, and all period acts are Manager/Owner |
| EXIT-002 | OWNER means proprietor, not passive investor | — | ADR-016 amendment | — | Documented | No investor role invented; boundary recorded |
| EXIT-003 | Out-of-scope objects answer 404 | organizations | `OutOfScope(ObjectDoesNotExist)` | `test_security.py::TestCrossOrganization`, `TestCrossBranch` | Done | Same code and wording as a missing row |
| EXIT-004 | In-scope without authority answers 403 | organizations | `PermissionMissing(PermissionDenied)` | `::test_9_a_branch_accountant_cannot_close_a_period` | Done | Reaching is weaker than scope |
| EXIT-005 | Idempotency keys are unique per organization | accounting | `journal_entry_idempotency_key_unique_per_organization` | `test_idempotency.py::TestKeysAreScopedToTheOrganization` | Done | **Fixes a cross-tenant leak** |
| EXIT-006 | A replay is verified against the request | accounting | `idempotency_fingerprint`, `_replay` | `::TestSameKeyDifferentRequest` | Done | `idempotency_key_conflict` |
| EXIT-007 | A key cannot reach another organization's journal | accounting | org-scoped lookup + selector | `::test_a_key_cannot_be_used_to_discover_another_organizations_journal` | Done | |
| EXIT-008 | `source_document_id` carries any identifier type | accounting | `CharField(max_length=64)` | — | Verified, unchanged | int, UUID, or external ref (ADR-017) |
| EXIT-009 | Losing one of two memberships keeps the role, drops the scope | organizations | `sync_user_role_groups` | `::TestMultiMembershipRecomputation` | Done | Branch and organization variants |
| EXIT-010 | Global permissions never substitute for object scope | organizations | `authorization.py` | `::test_a_global_permission_never_substitutes_for_scope` | Done | |
| EXIT-011 | Native foundation screens exist inside the shell | organizations, units, users, core | `urls.py` + `navigation.py` | `apps/core/tests/test_foundation_screens.py` | Done | Org, branch, access, users, units, audit |
| EXIT-012 | A fresh database migrates from zero and seeds | all | migrations + seed commands | Verified manually on `khan_mandi_freshcheck` | Done | 10 units, 46 accounts, 6 cost centres, 8 role groups |
| EXIT-013 | Seeding survives a non-UTF-8 console | core, units | `apps.core.console.SeedCommand` | `tests/test_phase_0_exit.py::test_seeding_survives_a_console_that_cannot_render_arabic` | Done | **Fixes a fresh-install failure** |
| EXIT-014 | The foundations cooperate end to end | all | — | `tests/test_phase_0_exit.py::TestTheFoundationsCooperate` | Done | Services and API, no ORM shortcuts |

## Phase 1 — Inventory (specified by Task 1.0, none implemented)

Every row below is **Specified**, not Done. Task 1.0 is specification only;
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
| INV-001 | Item code unique per organization; archived codes reserved | `UniqueConstraint(organization, code)` | `::test_item_code_unique_per_organization` | 1.1 | AT-008 | Specified |
| INV-002 | A foreign organization's item cannot be injected | `_scoped_item` resolver | `::test_foreign_item_injection_blocked` | 1.1 | AT-008 | Specified |
| INV-003 | A foreign branch's warehouse is unreachable (404) | `resolve_warehouse` via Phase 0 authorization | `::test_foreign_branch_warehouse_blocked` | 1.1 | AT-008 | Specified |
| INV-004 | Base UoM dimension validated against the entered unit | Reuses `units.services._require_same_dimension` | `::test_base_uom_dimension_validation` | 1.1 | | Specified |
| INV-005 | Base UoM immutable once movements exist | Service guard | `::test_base_unit_locked_after_movement` | 1.1 | | Specified |
| INV-006 | Fixed package conversion applies exactly | `ItemUnitConversion` `FIXED` | `::test_fixed_package_conversion` | 1.1 | | Specified |
| INV-007 | Variable package requires a measured base quantity | `VARIABLE` + `measured_quantity_required` | `::test_variable_weight_requires_measurement` | 1.1 | | Specified |
| INV-008 | Overlapping conversion periods refused | `EXCLUDE USING gist` | `::test_overlapping_conversion_refused` | 1.1 | | Specified |
| INV-009 | Conversion snapshot stays historical after the master changes | Factor + version stored on the movement | `::test_conversion_snapshot_is_historical` | 1.2 | AT-011 | Specified |
| INV-010 | No float in inventory storage or transport | `quantity.py` / `money.py`; string API decimals | `::test_no_float_transport` | 1.1 | | Specified |
| INV-011 | Arabic locale does not change technical decimal strings | Locale-independent rendering | `::test_locale_does_not_change_decimals` | 1.1 | | Specified |
| INV-012 | Posted stock movements are immutable | Allowlist trigger, per `accounting/0005` | `::test_posted_movement_immutable` | 1.2 | | Specified |
| INV-013 | Every movement carries the full required column set | Non-null columns | `::test_movement_records_everything` | 1.2 | | Specified |
| INV-014 | Valuation key is `(warehouse, item, lot)` | `UniqueConstraint` on `StockBalance` | `::test_valuation_key` | 1.2 | | Specified |
| INV-015 | Moving weighted average — all 18 cases | Valuation engine | `::TestMovingWeightedAverage` | 1.2 | | Specified |
| INV-016 | Quantity zero implies value zero | Full-depletion rule | `::test_full_depletion_leaves_no_residual` | 1.2 | AT-007 | Specified |
| INV-017 | `StockBalance` rebuilds exactly from the ledger | Rebuild command | `::test_rebuild_equals_ledger` | 1.2 | AT-007 | Specified |
| INV-018 | Negative stock refused by default | Service check inside the lock + trigger | `::test_negative_stock_blocked` | 1.2 | | Specified |
| INV-019 | Concurrent issues cannot create negative stock | `select_for_update` in deterministic order | `::test_concurrent_issue_cannot_go_negative` | 1.2 | | Specified |
| INV-020 | Negative-stock override needs permission, reason, actor, audit | `inventory.override_negative_stock` | `::test_unauthorized_override_rejected` | 1.2 | AT-008 | Specified |
| INV-021 | Closed-period movements refused | Reuses `validate_period_accepts_postings` | `::test_closed_period_movement_rejected` | 1.2 | AT-011 | Specified |
| INV-022 | Backdated valuation follows posting order, not effective date | Documented policy + test | `::test_backdated_does_not_restate_history` | 1.2 | AT-011 | Specified |
| INV-023 | COMMIT-boundary constraints exercised | `transaction=True` tests | `::test_commit_boundary` | 1.2 | | Specified |
| INV-024 | Audit captures authoritative before/after state | `record_audit_event` with DB re-read | `::test_audit_before_and_after` | 1.2 | | Specified |
| INV-025 | No hard-coded account ids in inventory posting | `AccountRole` + `AccountMapping` | `::test_no_hardcoded_accounts` | 1.3 | | Specified |
| INV-026 | Opening value equals its journal entry | Atomic opening posting | `::test_opening_equals_journal` | 1.3 | AT-002 | Specified |
| INV-027 | Inventory control reconciles to inventory valuation | Reconciliation report | `::test_inventory_reconciles_to_gl` | 1.3 | AT-002 | Specified |
| INV-028 | Duplicate source event cannot double-post | Source identity (ADR-017) | `::test_duplicate_source_event` | 1.3 | AT-009 | Specified |
| INV-029 | Same key + changed payload conflicts | Idempotency fingerprint | `::test_key_with_changed_payload_conflicts` | 1.3 | AT-009 | Specified |
| INV-030 | Same key in another organization is independent | Org-scoped key | `::test_key_independent_across_organizations` | 1.3 | AT-009 | Specified |
| INV-031 | Reversal restores quantity and value exactly | `REVERSAL` at the original's value | `::test_reversal_restores_exactly` | 1.4 | | Specified |
| INV-032 | `RETURN_IN` values at the original issue cost | Link to the issuing movement | `::test_return_in_uses_original_cost` | 1.4 | | Specified |
| INV-033 | Transfer dispatch reconciles to receipt plus shortage | In-transit accounting | `::test_transfer_reconciles` | 1.5 | AT-002 | Specified |
| INV-034 | Inter-branch transfer needs authority at both branches | `_require_at_every_branch` pattern | `::test_transfer_needs_both_branches` | 1.5 | AT-008 | Specified |
| INV-035 | Posting to a frozen warehouse refused | `freeze_state` guard | `::test_frozen_warehouse_refuses_posting` | 1.6 | | Specified |
| INV-036 | Conducting and approving a count are separate permissions | Two permissions | `::test_count_approval_is_separated` | 1.6 | | Specified |
| INV-037 | Import rollback is atomic | Import boundary | `::test_import_rollback_is_atomic` | 1.7 | AT-012 | Specified |
| INV-038 | Location quantities sum to warehouse quantity | Reconciliation test | `::test_locations_sum_to_warehouse` | 1.7 | | Specified |
| INV-039 | A fresh database receives inventory reference data | Seed command | `::test_fresh_database_seeds_inventory` | 1.7 | | Specified |
| INV-040 | No writable CRUD bypasses the posting services | Command API + read-only admin | `::test_no_crud_bypass` | 1.1 | AT-008 | Specified |

## Not yet mapped

The SRS has not been added to this repository. `docs/requirements/SRS.md` is
referenced by `CLAUDE.md` but does not exist. Until it is supplied, requirement
IDs above are local to the bootstrap and are not traceable to a business
source.
