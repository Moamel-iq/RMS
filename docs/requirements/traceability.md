# Requirements traceability

Maps each requirement to the code and tests that satisfy it. Updated as part of
every task's definition of done.

## Coverage — where each approved task's evidence lives

`tests/test_traceability.py` checks two different things, and the second one
exists because the first was not enough. It resolves every citation to a real
test **and** it checks that every approved task is represented here at all.
A task can pass the first check trivially by having no rows, which is exactly
how Task 0.6 — the accounting kernel every other module posts through — sat
untraced with a hundred passing tests behind it.

Most tasks are found automatically: by a `## Task X.Y` heading, or by their
number in a `Task` column. The tasks below are the ones that are not, and
each says why. A future task cannot be silently absent — it either appears
one of those two ways or it is declared here, deliberately, in a diff someone
reviews.

| Task | Evidence lives in | Why not its own section |
|---|---|---|
| 0.1 | The leading table — `ENV-`, `API-` | Bootstrap: settings, health, the API skeleton. Written before the document grew sections |
| 0.2 | The leading table — `USR-`, `UI-` | Custom user and the sign-in surface, same table |
| 0.3 | The leading table — `ORG-` | Organization, branch, membership, scope |
| 0.4 | The leading table — `UOM-` | Units of measure and conversion |
| 0.5 | The leading table — `AUD-` | The audit foundation |
| 1.0 | — | A specification task. It *produces* the Phase 1 requirements the rows below cite; it has no separate implementation to trace |
| 2.0 | — | The same, for Phase 2 plus Procurement completion (`PRC-001`..`PRC-075`) |
| 1.7 | Superseded by 1.7A and 1.7B | Split during Phase 1; both halves carry their own rows |
| 1.8 | `EXIT-` rows plus the Phase 1 gate record | An exit gate verifies other tasks' requirements rather than adding its own |
| 2.18 | The Phase 2 gate record | The same; see `docs/runbooks/overnight-progress.md` Step 20 |
| 3.0 | — | A specification task, for Phase 3 (`RCP-001`..`RCP-126`, extended by Tasks 3.0A and 3.0B); **approved by the owner 2026-08-16** |
| 3.11 | The Phase 3 gate record, when it runs | An exit gate verifies other tasks' requirements rather than adding its own |

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
| MON-011 | Cash rounding residual posts to an explicit account | core | `apply_cash_settlement_rounding` returns `(rounded, adjustment)`; account `7-09-01-001` seeded by `seed_chart_of_accounts` | `test_money.py::test_the_adjustment_always_reconciles`, `test_chart_of_accounts.py::test_the_cash_rounding_account_exists_though_the_policy_is_off` | Done | Was `Partial` pending the account; the account has existed since Task 0.6 (ADR-014) and is tested. Rounding itself stays **off** and posts nothing until a policy enables it |
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

## Task 0.6 — the accounting journal kernel

The twelve kernel invariants of `docs/specs/accounting-kernel-invariants.md`,
mapped to the tests that hold them. **One row per requirement, not per
test** — the four modules below carry a hundred tests between them, and a
row for each would be an index rather than a matrix.

This section was missing until the Phase 2 gate, and its absence is the
reason `tests/test_traceability.py` now checks *coverage* as well as
citations: the old check resolved each row to a real test and therefore
reported clean while the kernel — the thing every other module posts
through — had no rows at all. A hundred passing tests were invisible to the
matrix, which is the failure mode most likely to be mistaken for its
opposite.

| ID | Requirement | App | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|---|
| ACC-001 | Every posted entry balances, and the database refuses an unbalanced one even by raw SQL | accounting | `validators.validate_balanced` + `DEFERRABLE INITIALLY DEFERRED` trigger, migration `accounting.0001` | `test_posting.py::TestBalance`, `test_commit_boundary.py::test_an_unbalancing_line_is_refused_at_commit` | Done | The trigger fires at COMMIT, so a mid-transaction imbalance is legal and a committed one is impossible |
| ACC-002 | A line carries exactly one side, a positive amount, and never a float | accounting | `validators.validate_line_sides`, `CheckConstraint`s on `JournalLine` | `test_posting.py::TestLineShape` | Done | ADR-012 |
| ACC-003 | Only detail accounts accept postings; group and archived accounts are refused | accounting | `validators.validate_accounts_are_postable`, `is_postable` derived from the code | `test_posting.py::TestPostableAccounts`, `test_hardening.py::TestHierarchyExclusivity` | Done | A parent cannot acquire a child once it has history |
| ACC-004 | Cost-centre policy comes from the account, never from the caller | accounting | `validators.validate_cost_centers`, `Account.requires_cost_center` | `test_posting.py::TestCostCenterPolicy` | Done | ADR-015 |
| ACC-005 | One entry never mixes organizations — account, branch or cost centre | accounting | `validators.validate_organization_consistency` | `test_posting.py::TestOrganizationIsolation` | Done | The tenancy boundary at the ledger |
| ACC-006 | Journal numbers are gapless and scoped per organization and year | accounting | `services._next_entry_number` under a row lock on `JournalNumberSequence` | `test_posting.py::TestNumbering` | Done | MAX+1 would let two postings claim one number |
| ACC-007 | The period is resolved from the accounting date; CLOSED refuses everything and SOFT_CLOSED refuses routine postings | accounting | `validators.validate_period_accepts_postings`, `services.close_period` / `reopen_period` | `test_posting.py::TestPeriods`, `test_hardening.py::TestPeriodOrdering`, `test_hardening.py::TestSoftClosedAuthorization` | Done | ADR-013; periods close and reopen in order |
| ACC-008 | A posted entry and its lines are immutable, including against raw SQL | accounting | `accounting_journalentry_no_change` / `accounting_journalline_no_change` triggers, migrations `accounting.0002`, `0005` | `test_posting.py::TestImmutability`, `test_commit_boundary.py::test_deleting_one_line_of_a_posted_entry_is_refused` | Done | Allowlist of permitted columns, not a blocklist — see migration 0005 |
| ACC-009 | Correction is reversal: an exact mirror, once only, with a reason, the original preserved | accounting | `services.reverse_entry` | `test_posting.py::TestReversal`, `test_hardening.py::TestReversalErrorAccuracy` | Done | A second reversal reports already-reversed rather than posting again |
| ACC-010 | A failed posting leaves nothing behind | accounting | `@transaction.atomic` on `post_entry` | `test_posting.py::TestAtomicity` | Done | |
| ACC-011 | Balances are derived from posted lines, never stored | accounting | no balance column anywhere; derivation over `JournalLine` | `test_posting.py::TestTrialBalance` | Done | Trial balance debits equal credits by construction |
| ACC-012 | The same economic event posts once, and a key reused with a different payload is a conflict | accounting | `services._idempotency_fingerprint`, `_replay`, unique key per organization | `test_posting.py::TestIdempotency`, `test_idempotency.py` | Done | ADR-017; see also IDM-001..008 |
| ACC-013 | The chart seeds idempotently, with a code structure the database enforces | accounting | `seed_chart_of_accounts`, `account_code_matches_postable_flag` | `test_chart_of_accounts.py::TestSeed`, `test_chart_of_accounts.py::TestCodeStructure` | Done | ADR-014; 77 accounts and 6 cost centres |
| ACC-014 | Account and cost-centre codes are unique per organization and stay reserved after archiving | accounting | `account_code_unique_per_organization`, `on_delete=PROTECT` | `test_chart_of_accounts.py::TestScoping`, `test_chart_of_accounts.py::TestArchivingNotDeleting` | Done | Two organizations may reuse one code; a used code is never freed |
| ACC-015 | A statutory external mapping is complete or absent, and never affects posting | accounting | `account_external_mapping_is_complete_or_absent` | `test_chart_of_accounts.py::TestExternalMapping` | Done | Settles the second open question ADR-014 recorded |
| ACC-016 | Fiscal-year closure is derived from its periods, never stored | accounting | `FiscalYear.is_closed` property; no column | `test_hardening.py::TestFiscalYearClosureIsDerived` | Done | A stored flag would be a second opinion that goes stale |
| ACC-017 | An adjustment is a date plus a flag, valid anywhere in an open period; an entry needs both a debit and a credit and cannot be zero-valued | accounting | `is_adjustment`, `validators.validate_lines_present` | `test_hardening.py::TestAdjustmentSemantics`, `test_hardening.py::TestEntryShape` | Done | A two-line zero-value entry balances arithmetically and says nothing |
| ACC-018 | Closing a mapping's range and archiving an unused one are guarded by permission **and** organization scope | accounting | `AccountMappingCloseView`, `AccountMappingArchiveView` → `commands.close_account_role_mapping` / `archive_account_role_mapping` | `test_mapping_views.py::TestClosingAMapping`, `test_mapping_views.py::TestArchivingAMapping` | Done | Task 1.3 shipped both screens untested (ACCT-2). A mapping decides which GL account every module's postings land in; archiving is POST-only and template-less, so a regression would have been silent |
| ACC-019 | An unused mapping can be corrected through a screen, scoped to its own organization's accounts | accounting | `AccountMappingAmendView` → `commands.amend_account_role_mapping`; `AmendMappingForm` narrows the account queryset to the mapping's organization | `test_mapping_views.py::TestAmendingAMapping` | Done | ADR-019 lists amending beside closing and archiving, but Task 1.3 wired only the latter two — the command existed with no view, URL or route, so the documented correction path was a Python shell (ACCT-5) |
| ACC-020 | No account or cost-centre command endpoint exists; the chart is created by the seed command and managed in Phase 5 | accounting | `apps/accounting/api.py` exposes the seven routes Task 0.7 §3 lists and no chart routes; `manage_accounts` / `manage_cost_centers` gate `services.create_account` / `create_cost_center` | `test_api.py` | Documented | Task 0.7 §4's table implied endpoints that §3 never listed and §7 marked "Built"; `AccountAdmin` sent readers to them. Spec, mark and docstring corrected rather than endpoints invented (ACCT-5) |

## Task 0.7 — permissions, scope, API, idempotency

| ID | Requirement | App | Implementation | Test | Status | Notes |
|---|---|---|---|---|---|---|
| PRM-001 | The twelve named accounting permissions of Task 0.7 §1 exist, and a thirteenth arrived with Task 1.3 | accounting | `Meta.permissions` on `JournalEntry`, `Account`, `CostCenter`, `AccountingPeriod`, and `OrganizationAccountMapping` (`manage_account_mappings`, Task 1.3) | `test_permissions.py::TestThePermissionsExist` | Done | Not Django add/change/delete. The row said "twelve" while its own test asserted thirteen: twelve was right for 0.7, and the count moved when 1.3 added the mapping permission on a model this row did not name |
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
| EXIT-007 | A key cannot reach another organization's journal | accounting | `services._replay` / `_entry_for_source`, both organization-scoped | `test_idempotency.py::test_a_key_cannot_be_used_to_discover_another_organizations_journal` | Done | The implementation column named a selector that no production path calls; the scoping lives in the services |
| EXIT-008 | `source_document_id` carries any identifier type | accounting | `CharField(max_length=64)` | — | Verified | Unchanged. int, UUID, or external ref (ADR-017) |
| EXIT-009 | Losing one of two memberships keeps the role, drops the scope | organizations | `sync_user_role_groups` | `test_permissions.py::TestMultiMembershipRecomputation` | Done | Branch and organization variants |
| EXIT-010 | Global permissions never substitute for object scope | organizations | `authorization.py` | `test_permissions.py::test_a_global_permission_never_substitutes_for_scope` | Done | |
| EXIT-011 | Native foundation screens exist inside the shell | organizations, units, users, core | `urls.py` + `navigation.py` | `apps/core/tests/test_foundation_screens.py` | Done | Org, branch, access, users, units, audit |
| EXIT-012 | A fresh database migrates from zero and seeds | all | migrations + seed commands | Verified manually on `khan_mandi_freshcheck` | Verified | Manual, not automated. 10 units, 46 accounts, 6 cost centres, 8 role groups |
| EXIT-013 | Seeding survives a non-UTF-8 console | core, units | `apps.core.console.SeedCommand` | `tests/test_phase_0_exit.py::test_seeding_survives_a_console_that_cannot_render_arabic` | Done | **Fixes a fresh-install failure** |
| EXIT-014 | The foundations cooperate end to end | all | — | `tests/test_phase_0_exit.py::TestTheFoundationsCooperate` | Done | Services and API, no ORM shortcuts |

## Phase 1 — Inventory (specified by Task 1.0, delivered and tagged)

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
| INV-272 | Import batch row counts, branch/kind pairing and applied state hold at COMMIT | migration 0016 constraints | `test_import_constraints.py::TestImportConstraints` | 1.7A | | Done |
| INV-273 | A location carries quantity and never value | schema has no money columns | `test_locations.py::test_the_balance_model_has_no_money_columns` | 1.7B | | Done |
| INV-274 | A move between bins posts no stock movement and does not revalue | `move_between_locations` | `test_locations.py::test_a_move_between_bins_posts_no_stock_movement` | 1.7B | | Done |
| INV-275 | A put-away cannot exceed the unlocated remainder | `put_away` under the position lock | `test_locations.py::test_putting_away_more_than_is_unlocated_is_refused` | 1.7B | | Done |
| INV-276 | An issue naming no bin still leaves the invariant true | `release_for_outbound`, called by the ledger | `test_locations.py::test_an_issue_that_names_no_bin_still_leaves_the_invariant_true` | 1.7B | AT-007 | Done |
| INV-277 | Two concurrent put-aways cannot both take the same unlocated stock | `(warehouse, item, lot)` advisory lock | `test_location_concurrency.py::TestLocationConcurrency` | 1.7B | | Done |
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
| PRC-002 | Supplier code canonical uppercase, unique per organization, archived codes reserved | `create_supplier` + `UniqueConstraint` | `apps/procurement/tests/test_supplier.py::TestSupplierCode` | 2.1 | | Done |
| PRC-003 | `Supplier` carries no balance field; balances derive from posted documents | model shape | `apps/procurement/tests/test_supplier.py::test_there_is_no_balance_field`, `apps/procurement/tests/test_supplier.py::test_there_is_no_account_field` | 2.1 | | Done |
| PRC-004 | A supplier is archived, never deleted | `on_delete=PROTECT` | `apps/procurement/tests/test_supplier.py::test_archive_and_reactivate_keep_the_row`, `apps/procurement/tests/test_supplier.py::test_an_archived_code_stays_reserved` | 2.1 | | Done |
| PRC-005 | A catalogue price values nothing; no posting service reads it | AST boundary test | `apps/procurement/tests/test_supplier_catalogue.py::TestTheCatalogueValuesNothing` | 2.2 | | Done |
| PRC-006 | One preferred supplier per item; one preferred catalogue row per pair | partial unique index | `apps/procurement/tests/test_supplier_catalogue.py::TestPreferredSource` | 2.2 | | Done |
| PRC-007 | Catalogue effective periods cannot overlap | `EXCLUDE USING gist` | `apps/procurement/tests/test_supplier_catalogue.py::test_overlapping_periods_are_impossible` | 2.2 | | Done |
| PRC-008 | A catalogue package must be one the item can convert to base | service guard | `apps/procurement/tests/test_supplier_catalogue.py::TestPackageCompatibility` | 2.2 | | Done |
| PRC-009 | A purchase request has no stock and no accounting effect | asserted per status | `apps/procurement/tests/test_purchase_requests.py::TestNoLedgerEffect` | 2.3 | | Done |
| PRC-010 | A request approver is never its submitter | `CheckConstraint` | `apps/procurement/tests/test_purchase_requests.py::TestMakerChecker` | 2.3 | | Done |
| PRC-011 | Only a DRAFT request is editable | service guard + trigger | `apps/procurement/tests/test_purchase_requests.py::test_a_submitted_request_is_frozen`, `apps/procurement/tests/test_purchase_requests.py::test_a_line_cannot_be_removed_after_submission` | 2.3 | | Done |
| PRC-012 | Request lines snapshot conversion, version, factor and base quantity | non-null columns | `apps/procurement/tests/test_purchase_requests.py::TestLines` | 2.3 | | Done |
| PRC-013 | A quotation has no stock and no accounting effect | asserted per status | `apps/procurement/tests/test_quotations.py::TestNoLedgerEffect` | 2.4 | | Done |
| PRC-014 | Comparison normalises to base quantity and base unit price | comparison service | `apps/procurement/tests/test_comparison_and_award.py::TestTheRankingInversion` | 2.5 | | Done |
| PRC-015 | Freight is shown separately **and** inside a landed unit price | comparison report | `apps/procurement/tests/test_comparison_and_award.py::TestChargeAllocation`, `apps/procurement/tests/test_comparison_and_award.py::test_the_comparison_screen_shows_both_cheapest_flags` | 2.5 | | Done |
| PRC-016 | No automatic lowest-price award; a human names a reason | no auto-select path exists | `apps/procurement/tests/test_comparison_and_award.py::test_nothing_selects_a_winner_by_itself`, `apps/procurement/tests/test_comparison_and_award.py::test_an_award_without_a_reason_is_refused` | 2.5 | | Done |
| PRC-017 | An award records actor, reason, and the same-organization check | `award_quotation` | `apps/procurement/tests/test_comparison_and_award.py::TestTheAward` | 2.5 | | Done |
| PRC-018 | A purchase order creates no stock and no payable, including ISSUED | asserted per status | `apps/procurement/tests/test_purchase_orders.py::TestNoLedgerEffect` | 2.6 | | Done |
| PRC-019 | Issued terms are immutable; a change creates a version | allowlist trigger + version model | `apps/procurement/tests/test_order_change_control.py::TestRevision` | 2.7 | | Done |
| PRC-020 | A revision cannot reduce quantity below what was received | service guard under a lock | `apps/procurement/tests/test_goods_receipts.py::test_a_revision_below_the_received_quantity_is_now_refused`, `apps/procurement/tests/test_order_change_control.py::TestReceivedQuantityGuards` | 2.7 | | Done |
| PRC-021 | The supplier cannot change once a receipt exists | service guard | `apps/procurement/tests/test_order_change_control.py::test_the_supplier_cannot_be_revised_at_all` | 2.7 | | Done |
| PRC-022 | Cancellation needs a reason, is refused after a receipt, and is terminal | service guard | `apps/procurement/tests/test_goods_receipts.py::test_a_cancelled_order_cannot_receive`, `apps/procurement/tests/test_goods_receipts.py::test_a_stale_order_instance_cannot_bypass_the_cancellation_guard` | 2.7 | | Done |
| PRC-023 | Over-receipt is refused at zero tolerance | service guard under a lock | `apps/procurement/tests/test_goods_receipts.py::TestOrderRules` | 2.8 | | Done |
| PRC-024 | Delivered equals accepted plus rejected on every line | `CheckConstraint` | `apps/procurement/tests/test_goods_receipts.py::test_the_database_refuses_accepted_above_delivered` | 2.8 | | Done |
| PRC-025 | Only accepted quantity increases stock | posting service | `apps/procurement/tests/test_goods_receipts.py::TestADraftPostsNothing`, `apps/procurement/tests/test_goods_receipts.py::TestAcceptedAndRejected`, `apps/procurement/tests/test_goods_receipt_posting.py::test_only_the_accepted_quantity_reaches_stock_and_grni`, `apps/procurement/tests/test_goods_receipt_posting.py::test_a_wholly_rejected_line_posts_no_movement_beside_an_accepted_one` | 2.8, 2.9 | | Done |
| PRC-026 | A VARIABLE package line requires its measured quantity | reuses the inventory guard | `apps/procurement/tests/test_goods_receipts.py::test_a_variable_package_demands_the_scale_reading`, `apps/procurement/tests/test_goods_receipts.py::test_a_weighed_container_uses_the_scale_not_the_factor` | 2.8 | | Done |
| PRC-027 | Lot and expiry follow the rules of the item, unchanged | reuses `_validate_lot` | `apps/procurement/tests/test_goods_receipts.py::test_a_lot_tracked_item_requires_its_lot`, `apps/procurement/tests/test_goods_receipts.py::test_an_untracked_item_refuses_a_lot` | 2.8 | | Done |
| PRC-028 | A receipt without a purchase order is permitted, but never without a price | service guard | `apps/procurement/tests/test_goods_receipts.py::test_a_line_with_no_order_and_no_price_is_refused` | 2.8 | | Done |
| PRC-029 | Receipts post through the inventory kernel; no second posting path | `post_stock_entry` + `post_entry`, no third | `apps/procurement/tests/test_goods_receipts.py::test_posting_is_one_command_not_two`, `apps/procurement/tests/test_goods_receipt_posting.py::test_stock_cannot_exist_without_its_journal` | 2.8, 2.9 | | Done |
| PRC-030 | Partial receipt; cumulative accepted tracked against the order line | selector + guard | `apps/procurement/tests/test_goods_receipts.py::TestTheReceivedQuantitySeam`, `apps/procurement/tests/test_goods_receipt_posting.py::test_posting_consumes_the_ordered_quantity`, `apps/procurement/tests/test_goods_receipt_posting.py::test_two_partial_receipts_cannot_together_exceed_the_order` | 2.8, 2.9 | | Done |
| PRC-031 | A posted receipt is immutable; correction is reversal plus replacement | allowlist trigger | `apps/procurement/tests/test_goods_receipt_posting.py::test_a_posted_receipt_cannot_be_edited`, `apps/procurement/tests/test_goods_receipt_posting.py::test_a_posted_receipt_cannot_be_deleted`, `apps/procurement/tests/test_goods_receipt_posting.py::test_a_posted_line_is_frozen`, `apps/procurement/tests/test_goods_receipt_posting.py::test_a_reversal_mirrors_exactly` | 2.9 | | Done |
| PRC-032 | Receipt journal value equals receipt stock value, per line, to 3 dp | reconciliation verifier | `apps/procurement/tests/test_goods_receipt_posting.py::test_a_posted_receipt_reconciles`, `apps/procurement/tests/test_goods_receipt_posting.py::test_the_posting_service_exists_and_moves_both` | 2.9 | | Done |
| PRC-033 | Grouped debits where items resolve to different control accounts | reuses the opening-stock shape | `apps/procurement/tests/test_goods_receipt_posting.py::test_two_control_accounts_produce_two_debits_and_one_credit` | 2.9 | | Done |
| PRC-034 | No account, id or code is named in a posting service | effective-dated role resolution | `apps/procurement/tests/test_goods_receipt_posting.py::test_a_missing_mapping_rolls_everything_back`, `apps/procurement/tests/test_receipt_concurrency.py::test_posting_racing_a_mapping_change` | 2.9 | | Done |
| PRC-035 | Source identity is complete or absent, never partial | reuses the accounting guard | `apps/procurement/tests/test_goods_receipt_posting.py::test_the_source_identity_is_complete_and_unique` | 2.9 | | Done |
| PRC-036 | Document, movement, journal and status commit or roll back together | `transaction.atomic()` | `apps/procurement/tests/test_goods_receipt_posting.py::test_stock_cannot_exist_without_its_journal`, `apps/procurement/tests/test_goods_receipt_posting.py::test_a_missing_mapping_rolls_everything_back`, `apps/procurement/tests/test_goods_receipt_posting.py::test_a_closed_period_refuses_the_posting` | 2.9 | | Done |
| PRC-037 | Supplier invoice number unique per supplier over non-reversed invoices | partial unique index on a stored normalized key | `apps/procurement/tests/test_supplier_invoices.py::test_the_same_number_twice_for_one_supplier_is_refused`, `apps/procurement/tests/test_supplier_invoices.py::test_whitespace_does_not_create_a_second_invoice`, `apps/procurement/tests/test_supplier_invoices.py::test_case_does_not_create_a_second_invoice`, `apps/procurement/tests/test_supplier_invoices.py::test_leading_zeros_do_make_a_different_invoice`, `apps/procurement/tests/test_invoice_concurrency.py::test_two_invoices_racing_for_one_supplier_reference` | 2.10 | | Done |
| PRC-038 | An invoice never mutates stock | no inventory call exists in the path | `apps/procurement/tests/test_supplier_invoices.py::test_posting_moves_no_stock_at_all`, `apps/procurement/tests/test_supplier_invoices.py::test_a_reversal_moves_no_stock`, `apps/procurement/tests/test_supplier_invoices.py::test_the_grni_balance_is_untouched_by_an_invoice` | 2.10 | | Done |
| PRC-039 | Invoice total is the sum of its posted lines; charges allocated, never rated | `apps/core/allocation.py` largest remainder | `apps/procurement/tests/test_supplier_invoices.py::test_the_document_total_is_the_sum_of_its_lines`, `apps/procurement/tests/test_supplier_invoices.py::test_freight_and_discount_allocate_across_the_lines`, `apps/procurement/tests/test_supplier_invoices.py::test_an_indivisible_charge_leaves_no_residual`, `apps/procurement/tests/test_supplier_invoices.py::test_the_allocation_is_deterministic_across_runs` | 2.10 | | Done |
| PRC-040 | Allocation is many-to-many and partial | `PurchaseMatchAllocation` under a `PurchaseMatch` header | `apps/procurement/tests/test_matching.py::test_many_receipts_may_cover_one_invoice_line`, `apps/procurement/tests/test_matching.py::test_a_partial_allocation_leaves_a_remainder`, `apps/procurement/tests/test_matching.py::test_a_partial_allocation_takes_its_share`, `apps/procurement/tests/test_matching.py::test_a_duplicate_pair_in_one_match_is_refused` | 2.11 | | Done |
| PRC-041 | Over-allocation is impossible on both sides | availability derived from active rows, checked under a documented lock order + verifier | `apps/procurement/tests/test_matching.py::test_over_allocating_the_receipt_is_refused`, `apps/procurement/tests/test_matching.py::test_over_allocating_the_invoice_is_refused`, `apps/procurement/tests/test_matching.py::test_an_order_line_cannot_be_over_allocated`, `apps/procurement/tests/test_matching.py::test_cancelling_a_match_gives_the_quantity_back`, `apps/procurement/tests/test_matching_concurrency.py::test_two_matches_competing_for_one_receipt_remainder`, `apps/procurement/tests/test_matching_concurrency.py::test_two_matches_competing_for_one_invoice_remainder` | 2.11 | | Done |
| PRC-042 | Matching status is derived, never a stored mutable flag | `matching.invoice_line_match_state`; no status column on any line | `apps/procurement/tests/test_matching.py::test_the_derived_states_walk_from_unmatched_to_matched`, `apps/procurement/tests/test_matching.py::test_a_fully_matched_line_with_a_variance_is_an_exception`, `apps/procurement/tests/test_matching.py::test_the_queue_empties_as_a_delivery_is_matched` | 2.11 | | Done |
| PRC-043 | Price variance never restates a posted movement or a closed period | recognised where discovered, in a clearing account; no stock movement, no average change | `apps/procurement/tests/test_variance_posting.py::test_a_dearer_invoice_debits_the_difference`, `apps/procurement/tests/test_variance_posting.py::test_a_cheaper_invoice_credits_the_difference`, `apps/procurement/tests/test_variance_posting.py::test_an_agreeing_invoice_posts_two_lines_and_no_variance`, `apps/procurement/tests/test_variance_posting.py::test_the_moving_average_is_untouched_by_a_variance`, `apps/procurement/tests/test_variance_posting.py::test_posting_moves_no_stock_at_all` | 2.12 | | Done |
| PRC-044 | The on-hand versus consumed split is deterministic | `apps/core/allocation.py` | `apps/procurement/tests/test_variance_posting.py::test_no_revaluation_service_exists`, `apps/procurement/tests/test_variance_posting.py::test_no_revaluation_route_exists`, `apps/procurement/tests/test_variance_posting.py::test_no_revaluation_permission_exists` hold the boundary | 2.12 | **NOT ELECTED for Task 2.12.** Needs a permission, a source identity, an inventory-versus-COGS allocation policy, journal shapes both ways, and locking/reversal/period-close rules. None approved; ADR-022 §4 amended. | Deferred |
| PRC-045 | Release 1 parks the variance in a clearing account; revaluation is an explicit permissioned act | ADR-022, amended: clearing rather than expense | `apps/procurement/tests/test_variance_posting.py::test_the_variance_account_is_a_clearing_account`, `apps/procurement/tests/test_variance_posting.py::test_the_variance_balance_is_expected_to_stand`, `apps/procurement/tests/test_variance_posting.py::test_the_parked_variance_traces_to_its_allocations` | 2.12 | The parking half is done; the revaluation half is PRC-044, deferred. | Done |
| PRC-046 | Landed-cost capitalisation is captured but not implemented | fields present, no posting | — | 2.17 | Reassigned off 2.12: §16.2 excludes it from Release 1 and the 2.12 breakdown never named it. | Specified |
| PRC-047 | A supplier return is not an inventory `RETURN_IN` | `MovementType.RETURN_OUT`, outbound, kernel-owned | `apps/inventory/tests/test_ledger.py::test_it_is_outbound_and_is_not_return_in`, `apps/procurement/tests/test_supplier_returns.py::test_the_movement_is_return_out_and_not_return_in` | 2.13 | | Done |
| PRC-048 | A return leaves stock at the standing moving average | reuses the kernel; ADR-022 §1's worked example is the test | `apps/procurement/tests/test_supplier_returns.py::test_it_leaves_at_the_standing_average_not_the_receipt_price`, `apps/procurement/tests/test_supplier_returns.py::test_a_full_return_surrenders_the_whole_remaining_value` | 2.13 | | Done |
| PRC-049 | The average-versus-credit difference is a purchase return variance | ADR-022 §2 **as amended**: recognised at the credit note, never at the return | `apps/procurement/tests/test_supplier_returns.py::test_it_posts_no_return_variance`, `apps/procurement/tests/test_supplier_credit_notes.py::test_a_dearer_credit_recognises_the_gain`, `apps/procurement/tests/test_supplier_credit_notes.py::test_a_cheaper_credit_recognises_the_loss` | 2.13, 2.14 | The return refuses to guess; the note recognises against the agreed figure. | Done |
| PRC-050 | Negative stock is refused on a return, with no bypass | reuses `_require_available` | `apps/procurement/tests/test_return_concurrency.py::test_a_return_racing_an_issue_of_the_same_stock` | 2.13 | | Done |
| PRC-051 | A credit note reduces the payable or stands as unallocated credit; no stock | Both outcomes are allocation states against the payable; the whole note reduces what is owed | `apps/procurement/tests/test_supplier_credit_notes.py::test_an_allocation_reduces_the_invoice_outstanding`, `apps/procurement/tests/test_supplier_credit_notes.py::test_it_never_moves_stock` | 2.14 | Release 1 notes must cite a posted return — an invoice-only or reference-free note has no approved contra account and is not supported (ADR-022 §2 as amended). | Done |
| PRC-052 | Credit note document number unique per supplier over non-reversed notes | partial unique index over the folded key, the invoice's protection in reverse | `apps/procurement/tests/test_supplier_credit_notes.py::test_the_same_document_number_twice_is_refused` | 2.14 | | Done |
| PRC-053 | Partial payment across several invoices is normal | `PaymentAllocation` rows under one payment | `apps/procurement/tests/test_supplier_payments.py::test_partial_payment_across_two_invoices_is_normal` | 2.15 | | Done |
| PRC-054 | Payment over-allocation is impossible on both sides | checked at drafting, re-checked at posting under the invoice row locks, re-proved by reconciliation | `apps/procurement/tests/test_supplier_payments.py::test_over_allocating_the_invoice_is_refused`, `apps/procurement/tests/test_supplier_payments.py::test_allocations_may_not_exceed_the_payment`, `apps/procurement/tests/test_payment_concurrency.py::test_two_payments_racing_one_invoice_outstanding` | 2.15 | Bound is the invoice's outstanding net of credits and payments — stricter than "its total", deliberately. | Done |
| PRC-055 | An unallocated remainder is a supplier advance, never a negative payable | `Dr SUPPLIER_ADVANCE` (`1-04-01-001`) + `verify_supplier_payments` | `apps/procurement/tests/test_supplier_payments.py::test_the_remainder_stands_as_an_advance_never_a_negative_payable`, `apps/procurement/tests/test_supplier_payments.py::test_an_unallocated_payment_is_wholly_an_advance` | 2.15 | | Done |
| PRC-056 | Cash and bank come from effective-dated roles, never an id | `SUPPLIER_PAYMENT_CASH` / `SUPPLIER_PAYMENT_BANK` resolved by `method` | `apps/procurement/tests/test_supplier_payments.py::test_the_method_chooses_the_source_through_its_role` | 2.15 | | Done |
| PRC-057 | Oldest-invoice allocation is a visible default, never silent | the allocation form orders by due date; the service and API require explicit allocations | `apps/procurement/tests/test_supplier_payments.py::test_the_api_drives_the_lifecycle_and_money_is_strings` | 2.15 | | Done |
| PRC-058 | Procurement-to-GL reconciliation proves four equalities | `verify_procurement_accounting` (equalities 1–3), composed under `verify_procurement` with the per-document verifiers that re-check equality 4 (allocation bounds, single live posting) | `apps/procurement/tests/test_procurement_reports.py::TestProcurementToGl` | 2.16 | | Done |
| PRC-059 | Verification reports and refuses to repair | no repair path in `reconciliation.py` or `reports.py` | `apps/procurement/tests/test_procurement_reports.py::TestProcurementToGl::test_a_planted_journal_is_reported_by_verifier_and_report_alike` | 2.16 | | Done |
| PRC-060 | Receipt and return permissions are warehouse-scoped; money is organization-scoped | `PERMISSION_SCOPE` | `apps/procurement/tests/test_supplier.py::test_every_permission_declares_a_scope` | 2.1–2.15 | | Done |
| PRC-061 | Cost columns are omitted, not blanked, without `view_supplier_cost` | view layer | `apps/procurement/tests/test_supplier.py::test_a_storekeeper_sees_suppliers_and_never_their_prices` | 2.8 | | Done |
| PRC-062 | No writable CRUD API and no writable admin for a posted record | command API + read-only admin | `apps/procurement/tests/test_supplier.py::TestAdminIsReadOnly` | 2.17 | | Done |
| PRC-063 | API money and quantities are exact strings in both directions | schema layer | `apps/procurement/tests/test_supplier.py::TestScreens` | 2.1–2.15 | | Done |
| PRC-064 | Every command carries an organization-scoped idempotency key and fingerprint | reuses ADR-017 | — | 2.1–2.15 | | Specified |
| PRC-065 | Arabic RTL screens, logical properties, HTMX filters surviving pagination | templates + `_filter_query`; reports reuse the Phase 1 `_base_report.html` chrome | `apps/procurement/tests/test_procurement_reports.py::TestExports::test_the_htmx_request_gets_the_fragment_not_the_shell` | 2.1–2.16 | | Done |
| PRC-066 | Demo data: three suppliers, the five existing items, idempotent, DEBUG-only | demo tooling | `apps/procurement/tests/test_supplier.py::TestDemoSuppliers` | 2.1–2.17 | | Done |
| PRC-067 | `source_document_id` is the immutable `public_id`, never a number or pk | posting services | — | 2.9 | | Specified |
| PRC-068 | Supplier credit terms are effective-dated versions; the supplier integer is a non-editable compatibility projection | `SupplierCreditTerm`, supplier form/API/service | `apps/procurement/tests/test_supplier.py::TestOwnershipAndLifecycle::test_payment_terms_are_a_projection_not_an_editable_supplier_field` | Procurement completion 3 | | Done |
| PRC-069 | Inclusive active ranges cannot overlap and one supplier has at most one draft | migration 0033 exclusion/partial unique constraints | `apps/procurement/tests/test_credit_terms.py::TestCreditTermLifecycle::test_inclusive_active_ranges_cannot_overlap`, `test_only_one_draft_may_exist_per_supplier` | Procurement completion 3 | | Done |
| PRC-070 | A creator cannot activate their own term; activation is locked against stale instances | `activate_credit_term` plus database maker-checker check | `apps/procurement/tests/test_credit_terms.py::TestCreditTermLifecycle::test_creator_cannot_activate_their_own_term`, `test_stale_instance_cannot_be_activated_twice` | Procurement completion 3 | | Done |
| PRC-071 | Activated/superseded versions are immutable and not hard-deleted; correction is a replacement version | migration 0033 trigger and lifecycle services | `apps/procurement/tests/test_credit_terms.py::TestCreditTermLifecycle::test_database_rejects_mutating_an_activated_term`, `test_only_a_draft_can_be_edited_or_deleted` | Procurement completion 3 | | Done |
| PRC-072 | Invoice approval snapshots term UUID/version/name/days and due date from the invoice-date version | `approve_supplier_invoice` and invoice snapshot constraint | `apps/procurement/tests/test_credit_terms.py::TestInvoiceCreditTermSnapshot::test_approval_freezes_the_effective_term_and_due_date` | Procurement completion 3 | | Done |
| PRC-073 | A superseded term remains authoritative for its closed historical effective period | `resolve_credit_term` | `apps/procurement/tests/test_credit_terms.py::TestCreditTermLifecycle::test_superseded_version_remains_authoritative_for_its_closed_period` | Procurement completion 3 | | Done |
| PRC-074 | Credit-term UI/API use the same scoped lifecycle; foreign ids are 404 and in-scope missing authority is 403 | selectors, views, Ninja command endpoints | `apps/procurement/tests/test_credit_terms.py::TestCreditTermSurface` | Procurement completion 3 | | Done |
| PRC-075 | Demo suppliers expose exactly the 0/14/30-day terms and a second seed creates no duplicate term | `seed_demo_suppliers` through `create_supplier` bootstrap | `apps/procurement/tests/test_supplier.py::TestDemoSuppliers::test_the_seed_creates_exactly_three_and_is_idempotent` | Procurement completion 3 | | Done |
| PRC-076 | Supplier invoice UI/API expose the existing four-state aggregate without a parallel model; only DRAFT accepts PATCH/edit and return-to-draft is an audited command | `SupplierInvoiceUpdateView`, `PATCH /supplier-invoices/{id}/`, lifecycle services and trigger | `apps/procurement/tests/test_supplier_invoices.py::TestScopeAndPermissions::test_a_draft_header_has_an_htmx_edit_path`, `TestApi::test_patch_updates_only_a_draft_header` | Procurement completion 1 | | Done |
| PRC-077 | Invoice workspaces expose supplier/branch/date/due/matching filters and the complete PO→receipt→match→invoice→journal→credit/payment evidence chain | list/detail read model and HTMX templates | `apps/procurement/tests/test_supplier_invoices.py::TestScopeAndPermissions::test_the_workspace_filters_reference_supplier_branch_and_matching`, `test_a_posted_detail_shows_the_journal_lines` | Procurement completion 1 | | Done |
| PRC-078 | Direct invoice account choices exclude role-owned system accounts and the service independently rejects protected classes/mappings | `InvoiceAccountLineForm`, `_validate_direct_account`, `_refuse_role_owned_account` | `apps/procurement/tests/test_supplier_invoices.py::TestScopeAndPermissions::test_the_direct_account_selector_omits_role_owned_accounts` plus direct-account domain tests | Procurement completion 1 | | Done |
| PRC-079 | Restricted invoice HTML and JSON omit monetary fields rather than serializing blank/null placeholders | template cost branches and raw restricted API response | `apps/procurement/tests/test_supplier_invoices.py::TestScopeAndPermissions::test_cost_is_absent_from_restricted_html`, `TestApi::test_cost_fields_are_omitted_from_the_restricted_api` | Procurement completion 1 | | Done |

## Phase 3 — Recipes, Kitchen and Production

Established by `docs/tasks/task-3-0-recipes-production-domain-spec.md`
(2026-08-16), **approved by the owner on 2026-08-16** (spec §22.1). Every row is
`Specified` until its task
lands: Task 3.0 wrote no code, and a row that claimed evidence today would be
claiming a test that does not exist — which is the failure
`tests/test_traceability.py` was written to stop.

**Extended by Task 3.0A (2026-08-16) from 57 rows to 116.** RCP-058 – RCP-116
cover the source audit's data gate, structured recipe steps, nested
sub-recipes, servings, the corrected consumption partition, the Release 1
production boundary and the profitability boundary. RCP-046 is **amended**: its
original text described the architecture charter's actual-consumption formula,
which double-counts against this system's documents; it now points at its
correction.

**Extended again by Task 3.0B (2026-08-16) from 116 rows to 126.** The owner
supplied the Arabic recipe book and two plate-card decks; all 89 pages were
audited (spec §24). RCP-117 – RCP-126 cover the three-source boundary, row-level
provenance, measurement basis, conflict retention, the two-layer serving rule
and the ten-condition Task 3.10 data gate. Three owner decisions closed against
the source — KD-03, KD-05 and KD-06 — and two opened, KD-19 and KD-20.

Requirement identifiers are **repository-local**. No SRS exists to map them
to; see Task 3.0 §0 — which also records that the `KhanMandiRecipe.xlsx`
workbook (form `KM-RCP-004`) **does** exist outside the repository, was read in
full by Task 3.0A, and is authoritative about structure while carrying no data.

**Extended by Task 3.3 (2026-08-17) to RCP-140.** RCP-135 - RCP-140 record what
costing settled. RCP-023 - RCP-027, RCP-078, RCP-086, RCP-092 and RCP-093 move
from **Specified** to **Done**, plus the standard half of RCP-087. RCP-052 and
RCP-071 are **Partial**, each because a named later task owns the rest of the
clause.

RCP-087 is **split into RCP-087a and RCP-087b** rather than left Partial: the
standard recipe allocation is finished here and the posted-batch allocation
needs production. One row saying "Partial" hid a completed requirement behind an
unstarted one.

**Extended by Task 3.2B (2026-08-16) to RCP-134.** RCP-132 - RCP-134 record what
the nested-recipe graph settled: that a frozen exact reference outlives the
child's supersession, why a race cannot close a cycle here, and the
multiplier's precision.
RCP-070, RCP-072, RCP-074, RCP-076 and RCP-077 move from **Specified** to
**Done**.

**Extended by Task 3.2A (2026-08-16) to RCP-131.** RCP-127 – RCP-131 record what
the approval boundary settled that the specification had not: the six-state
lifecycle and the two states it deliberately does not have, the four-signature
evidence model without a global `CHEF` role, the separations that are actually
enforced, materialised organization-wide scope, and the inclusive range
convention. RCP-011 – RCP-014 move from **Specified** to **Done**; RCP-015 is
**Partial**, because "only approved versions are produced from" cannot finish
until production exists at Task 3.5.

| ID | Requirement | Implementation | Test | Task | AT | Status |
|---|---|---|---|---|---|---|
| RCP-001 | Phase 3 adds exactly one stock-moving document, the production batch; kitchen flows stay Phase 1 documents | reuse, not copies | — | 3.5 | | Specified |
| RCP-002 | Recipe and version edits touch no stock and no journal | apps/kitchen/models.py | `apps/kitchen/tests/test_boundary_and_security.py::TestZeroLedgerEffect` | 3.1 |  | Done |
| RCP-003 | No model combines two events in one row | separate aggregates | — | 3.1–3.7 | | Specified |
| RCP-004 | One new app `apps.kitchen`; nothing imports it until Phase 4 | apps/kitchen app boundary | `apps/kitchen/tests/test_boundary_and_security.py::TestDependencyDirection` | 3.1 |  | Done |
| RCP-005 | The module owns no role overrides, no second posting path, no menu item | apps/kitchen/models.py | `apps/kitchen/tests/test_boundary_and_security.py::TestDependencyDirection` | 3.1–3.9 |  | Done |
| RCP-006 | Recipes are organization master data | Recipe.organization | `apps/kitchen/tests/test_recipe_master.py::TestCodeIsIdentity` | 3.1 |  | Done |
| RCP-007 | Two recipe kinds, one model: batch (stored output item) and portion (no output item) | `recipe_type` + nullable `output_item` | `apps/kitchen/tests/test_recipe_master.py::TestTheOutputItemRule` | 3.1 |  | Done |
| RCP-008 | A batch recipe's output is `SEMI_FINISHED` or `FINISHED_GOOD`, same organization | `recipe_output_item_matches_type` | `apps/kitchen/tests/test_recipe_master.py::TestTheOutputItemRule` | 3.1 |  | Done |
| RCP-009 | A recipe carries no cost field | no cost field on Recipe | `apps/kitchen/tests/test_recipe_master.py::TestNoMoneyAnywhere` | 3.1 |  | Done |
| RCP-010 | Menu-item mapping is the recipe's identity; Phase 4 holds the FK | Recipe.public_id | `apps/kitchen/tests/test_recipe_master.py::TestCodeIsIdentity` | 3.1 |  | Done |
| RCP-011 | Versions are effective-dated; resolution by date and branch is the only resolution | `apps/kitchen/lifecycle.resolve_recipe_version` | `apps/kitchen/tests/test_effective_dating.py::TestRangeBoundaries` | 3.2A |  | Done |
| RCP-012 | Approved versions never overlap per branch | `recipe_scope_no_overlapping_ranges` | `apps/kitchen/tests/test_version_concurrency.py::TestTwoActivationsCannotOverlap` | 3.2A |  | Done |
| RCP-013 | Version approval is maker-checker | `recipe_version_approver_is_not_the_author` + `approve_recipe_version` | `apps/kitchen/tests/test_version_lifecycle.py::TestApproval` | 3.2A |  | Done |
| RCP-014 | Approved versions are immutable except range-close and supersession | `kitchen_recipe_version_is_immutable` + five child-table triggers | `apps/kitchen/tests/test_version_immutability.py::TestTheDatabaseRefusesRawWrites` | 3.2A |  | Done |
| RCP-015 | Only approved versions are produced from, costed, or counted | `RESOLVABLE_VERSION_STATUSES` excludes `APPROVED` | `apps/kitchen/tests/test_effective_dating.py::TestStatusIsNotResolution` | 3.2A – 3.8 |  | Partial |
| RCP-016 | Version numbers per-recipe sequential; supersession closes the prior range atomically | service | — | 3.2 | | Specified |
| RCP-017 | Branch applicability restricts; no per-branch line overrides | RecipeBranch through model | `apps/kitchen/tests/test_recipe_master.py::TestBranchApplicability` | 3.2 |  | Done |
| RCP-018 | Line quantities are gross; loss/yield rates are informational | model semantics | — | 3.1 | | Specified |
| RCP-019 | Line quantities persist at 6 dp, converted once at entry, quantized once | CALCULATION_PLACES on every quantity | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1 |  | Done |
| RCP-020 | Lines name items, never lots | RecipeLine.item, organization-checked | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1 |  | Done |
| RCP-021 | Optional lines are costed and omittable per batch | RecipeLine.is_optional | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1 / 3.3 |  | Done |
| RCP-022 | Substitutes are informational; costing uses actual consumption | `RecipeLineSubstitute` approves **items**, never conversions; a batch records what was consumed | `apps/kitchen/tests/test_draft_structure.py::TestSubstitutes` · `apps/kitchen/tests/test_production_services.py::TestSubstitution` | 3.1 / 3.4 | An approved stand-in may be in another dimension; the two are reported separately and never summed (spec §28.3) | Done |
| RCP-023 | Version cost **and plate cost** are derived from an exact version, warehouse and date; never stored | `apps/kitchen/costing.py` — plate cost from the primary `RecipeServing` | `apps/kitchen/tests/test_costing.py::TestDirectLines` · `apps/kitchen/tests/test_cost_plate.py::TestPlateCost` · `apps/kitchen/tests/test_cost_boundary.py::TestTheTaskBoundary::test_no_cost_field_was_added_to_recipe_or_version` | 3.3 | No `portions_per_batch` column exists; the primary serving is the divisor (spec §27.11) | Done |
| RCP-024 | A recipe cost equals the sum of its lines, to the fils; no cached figure may disagree | largest-remainder allocation + a check constraint | `apps/kitchen/tests/test_cost_snapshots.py::TestWhatIsRecorded::test_the_lines_sum_to_the_header_total` · `apps/kitchen/tests/test_costing.py::TestDirectLines::test_class_totals_add_up_to_the_document_total` | 3.3 | | Done |
| RCP-025 | Cost snapshots are explicit, dated and append-only | `kitchen/0009` triggers on all three tables | `apps/kitchen/tests/test_cost_snapshots.py::TestAppendOnly` | 3.3 | | Done |
| RCP-026 | Historical cost resolves version first, then costs at the same date; never silently today | `cost_recipe_on_date` | `apps/kitchen/tests/test_costing.py::TestPreviewAndAuthority::test_the_historical_read_resolves_the_version_first` · `::test_no_costing_entry_point_defaults_a_date` | 3.3 | | Done |
| RCP-027 | Recipe cost visibility is a separate permission | `view_recipe_cost`, organization-scoped | `apps/kitchen/tests/test_cost_security.py::TestTheApprovedRoleMap` · `::TestTheScreens` | 3.1 / 3.3 | | Done |
| RCP-028 | Batches draft from an approved version, scaled; no ad-hoc production | `create_production_batch` resolves one exact version from branch + explicit date and freezes it | `apps/kitchen/tests/test_production_contracts.py::TestIdempotencyAndVersionRaces` · `apps/kitchen/tests/test_production_scale.py::TestCreationRescaleAndPreviewAgree` | 3.4 | | Done |
| RCP-029 | One warehouse per batch | `_validate_shape` + `kitchen_productionbatch_warehouse_in_branch` | `apps/kitchen/tests/test_production_contracts.py::TestTheDecisionIsFrozen` · `apps/kitchen/tests/test_production_surface.py::TestAuthorizationIsNeverTheButton` | 3.4 | | Done |
| RCP-030 | Consumed quantities record reality; mismatch is variance, never refusal | `ProductionBatchActualLine` as a separate table; no service refuses a variance | `apps/kitchen/tests/test_production_services.py::TestActualConsumption::test_a_variance_is_recorded_and_never_refused` · `::TestSubstitution` | 3.4 | Normalized out of the sketched `consumed_quantity` column; spec §28.2 | Done |
| RCP-031 | Output quantity is entered, positive, beside the expected figure | `record_production_output`; nothing derives it from the inputs | `apps/kitchen/tests/test_production_services.py::TestOutputAndNotes::test_the_output_is_entered_and_never_derived` | 3.4 | | Done |
| RCP-032 | Only batch recipes are producible | `_validate_shape` refuses a recipe with no `output_item`; `draftable_recipes` never offers one | `apps/kitchen/tests/test_production_services.py::TestOnlyBatchRecipesAreProducible` | 3.4 | | Done |
| RCP-033 | Draft batches editable and deletable; posted immutable except reversal | `kitchen/0011` and `kitchen/0018` whole-row triggers branching on status, plus DELETE guards | `apps/kitchen/tests/test_production_contracts.py::TestTheDecisionIsFrozen` · `apps/kitchen/tests/test_production_posting.py::TestPostedImmutability` · `apps/kitchen/tests/test_production_posting_verifier.py::TestPlantedDefects` | 3.4 / 3.5 | 0018 replaced 0011's `status`/`number` escape hatch with a POSTED branch whose allowlist is the reversal columns and nothing else | Done |
| RCP-034 | Value is conserved: output enters at Σ consumed values via `inbound_value` | `apps/inventory/production.py` projects with the kernel's own `apply_outbound`, posts once, then asserts the projection against the movements; `production_batch_conserves_value` refuses the row | `apps/kitchen/tests/test_production_posting.py::TestValueConservation` · `apps/kitchen/tests/test_production_posting_verifier.py::TestPlantedDefects::test_the_database_refuses_to_break_value_conservation` | 3.5 | | Done |
| RCP-035 | Yield loss is absorbed into unit cost, never posted | the output's unit cost is Σ consumed ÷ actual output; no yield account and no yield journal exist | `apps/kitchen/tests/test_production_posting.py::TestValueConservation::test_yield_loss_lands_in_the_unit_cost_and_nowhere_else` | 3.5 | | Done |
| RCP-036 | The journal is the per-account net; zero-net batches post no journal | `_journal_for` nets per control account and returns `None` when nothing is left to say | `apps/kitchen/tests/test_production_posting.py::TestTheJournalAndItsSilence` · `apps/kitchen/tests/test_production_posting_verifier.py::TestTheJournalAndItsSilence` | 3.5 | The absent journal is proved absent **for the right reason** by recomputing the nets, not by reading the column | Done |
| RCP-037 | Non-zero nets debit/credit exactly the movements' accounts, branches, values | one balanced entry per non-zero net, with full source identity, linked to the stock entry | `apps/kitchen/tests/test_production_posting.py::TestTheJournalAndItsSilence::test_a_separate_output_account_writes_a_netted_balanced_journal` · `apps/kitchen/tests/test_production_posting_verifier.py::TestTheJournalAndItsSilence::test_a_journal_that_should_have_been_silent_is_caught` | 3.5 | | Done |
| RCP-038 | Produced lots record `produced_by_*` and expire by shelf life | `_output_lot` writes the fields Phase 1 reserved; expiry runs from the batch's business date, never today | `apps/kitchen/tests/test_production_posting.py::TestTheOutputLot` · `apps/kitchen/tests/test_production_posting_verifier.py::TestPlantedDefects::test_a_missing_output_lot_is_caught` | 3.5 | | Done |
| RCP-039 | Expired ingredients cannot enter a batch | `PRODUCTION_OUT` stays out of `EXPIRED_LOT_REMOVAL_TYPES`; the kernel refuses | `apps/inventory/tests/test_ledger.py` (kernel rule, unchanged) | 3.5 | Kept by **not** extending the kernel's exemption set — the rule's stated purpose is stopping expired food reaching a kitchen, and production is that path | Done |
| RCP-040 | Reversal mirrors exactly, checks availability, once, with reason; ADR-017 identity unchanged | `reverse_production_batch` through the kernel's own mirror; a batch with no journal writes no reversal journal | `apps/kitchen/tests/test_production_posting.py::TestReversal` · `apps/kitchen/tests/test_production_posting_verifier.py::TestReversal` · `apps/kitchen/tests/test_production_posting_races.py::TestTheOutputAndItsReversal` | 3.5 | | Done |
| RCP-041 | Yield, loss and batch variance are reads | report queries | — | 3.6 | | Specified |
| RCP-042 | Version rates display beside measured yield | yield report | — | 3.6 | | Specified |
| RCP-043 | Meal records move no stock and post no journal; they explain, not consume | model shape | — | 3.7 | | Specified |
| RCP-044 | Staff-meal expense reclassification is deferred, recorded, data accumulating | deferral note | — | 3.7 | | Specified |
| RCP-045 | Meal corrections are cancellation, never edits | status machine | — | 3.7 | | Specified |
| RCP-046 | **Amended by 3.0A.** Actual consumption is not one formula; it is two reads (RCP-098 – RCP-107) | superseded derivation | — | 3.8 | | Specified |
| RCP-047 | Theoretical consumption uses effective versions over recordable sources; Phase 4 plugs sales in | quantity-source socket | — | 3.8 | | Specified |
| RCP-048 | The backflush election is not made here; Phase 4 records it if elected | documented boundary | — | 3.8 | | Specified |
| RCP-049 | `verify_kitchen` proves batch agreement, value conservation, identity | the verifier | — | 3.9 | | Specified |
| RCP-050 | Verification reports and refuses to repair | no repair path | — | 3.9 | | Specified |
| RCP-051 | Production permissions are warehouse-scoped; recipes organization-scoped | `PERMISSION_SCOPE` | — | 3.1–3.5 | | Specified |
| RCP-052 | Cost columns omitted, not blanked, without `view_recipe_cost` | view + API layer, asserted on raw bytes | `apps/kitchen/tests/test_cost_security.py::TestTheApi::test_an_unauthorized_caller_gets_no_cost_key_at_all` · `::TestTheScreens::test_the_version_detail_page_omits_the_cost_link_without_the_permission` | 3.3 / 3.9 | Costing screens only; later reports at 3.9 | Partial |
| RCP-053 | No writable CRUD or admin for posted records | read-only admin | `apps/kitchen/tests/test_boundary_and_security.py::TestAdminIsReadOnly` | 3.10 |  | Done |
| RCP-054 | API is commands; money and quantities as exact strings; ADR-017 keys | schema layer | — | 3.1–3.9 | | Specified |
| RCP-055 | Arabic RTL screens in the shell; sections go live task by task | templates/kitchen/ | `apps/kitchen/tests/test_boundary_and_security.py::TestScreens` | 3.1–3.10 |  | Done |
| RCP-056 | Demo: two named recipes, one permitted new item, posted and draft batches, meals, every screen showing something | apps/kitchen/demo.py | `apps/kitchen/tests/test_demo_and_concurrency.py::TestDemoDataset` | 3.1–3.10 |  | Done |
| RCP-057 | Recipe imports are preview-first master data; nothing that posts imports | Task 1.7 framework registration | — | 3.10 | | Specified |
| RCP-058 | The workbook's structure is source; its data is not. No import or demo counts as real recipes until a filled `KM-RCP-004` exists | data gate + `DEMO` namespace | — | 3.10 | | Specified |
| RCP-059 | No quantity, loss, yield, cost or price is presented as a Khan Mandi figure without a traceable source | symbols and labelled illustrations | — | 3.1–3.10 | | Specified |
| RCP-060 | Loss is recorded per ingredient line as well as per version; both informational | RecipeLine.loss_rate | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1 |  | Done |
| RCP-061 | Every line carries a cost class FOOD / PACKAGING / ACCOMPANIMENT; a reporting dimension only | RecipeLine.cost_class | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1 |  | Done |
| RCP-062 | Measured and approved quantities are separate facts; costing reads only the approved one | measured_quantity beside base_quantity | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1 |  | Done |
| RCP-063 | The method is a sequence of structured steps; free-text instructions is an overview only | RecipeStep | `apps/kitchen/tests/test_draft_structure.py::TestSteps` | 3.1 |  | Done |
| RCP-064 | Steps are frozen with the approved version, like lines | allowlist trigger | — | 3.2 | | Specified |
| RCP-065 | Step sequence is explicit, positive and unique per version; gaps are legal | `recipe_step_sequence_unique_per_version` | `apps/kitchen/tests/test_draft_structure.py::TestSteps` | 3.1 |  | Done |
| RCP-066 | Steps carry no arithmetic; no costing or consumption read touches them | RecipeStepIngredient | `apps/kitchen/tests/test_draft_structure.py::TestSteps` | 3.1–3.9 |  | Done |
| RCP-067 | Step ingredient share is in (0, 1] and sums to at most 1 per line | share bound + per-line sum | `apps/kitchen/tests/test_draft_structure.py::TestStepIngredientLinks` | 3.1 |  | Done |
| RCP-068 | Duration and temperature stay null unless a source supplies them | nullable duration and temperature | `apps/kitchen/tests/test_draft_structure.py::TestSteps` | 3.1 |  | Done |
| RCP-069 | Arabic is the source language for instructions and checkpoints | instruction_ar required | `apps/kitchen/tests/test_draft_structure.py::TestSteps` | 3.1 |  | Done |
| RCP-070 | Stocked and non-stocked sub-recipes are mutually exclusive **by construction** | `recipe_component_child_is_stocked` | `apps/kitchen/tests/test_components.py::TestTheTwoShapes` | 3.2B | Done |
| RCP-071 | A stocked sub-recipe is consumed at book value; its tree is never re-expanded | `expansion.expand_recipe_version` treats it as a leaf, for costing **and** drafting | `apps/kitchen/tests/test_cost_nesting.py::TestStockedSubRecipesAreNotExpanded` · `apps/kitchen/tests/test_production_services.py::TestTheDemoProductionDraft::test_the_draft_shows_every_shape_the_task_asks_for` | 3.3 / 3.4 | Posting half arrives at 3.5 | Partial |
| RCP-072 | A component references one exact approved child version; no silent re-pointing | `exact child version, never re-pointed` | `apps/kitchen/tests/test_components.py::TestExactVersionAdoption` | 3.2B | Done |
| RCP-073 | Quantities scale multiplicatively down the tree and quantize once | `production.scaled_line_quantity`, mirrored by the `kitchen/0015` trigger | `apps/kitchen/tests/test_production_scale.py::TestThePythonAndSqlArithmeticAgree` · `::TestCreationRescaleAndPreviewAgree` | 3.4 | The cumulative multiplier is rounded once at its own column, and every consumer reads the stored figure — see ADR-024's Task 3.4 amendment | Done |
| RCP-074 | A child version's effective range covers the parent's, for every applicable branch | `require_effective_coverage` | `apps/kitchen/tests/test_component_coverage.py::TestCoverageAtActivation` | 3.2B | Done |
| RCP-075 | Parent and child share an organization; the child's branches are a superset | approval validation | — | 3.2 | | Specified |
| RCP-076 | Cycles are rejected at any depth, on draft save and again at approval | `cycle_path at recipe identity` | `apps/kitchen/tests/test_components.py::TestCycles` | 3.2B | Done |
| RCP-077 | Nesting depth is a validated constant with a named error; default 3 | `MAX_COMPONENT_DEPTH = 3` | `apps/kitchen/tests/test_components.py::TestDepth` | 3.2B | Done |
| RCP-078 | Component cost rolls up recursively and quantizes once, at the top | `apps/kitchen/costing.py` | `apps/kitchen/tests/test_cost_nesting.py::TestNestedRollUp` | 3.3 | | Done |
| RCP-079 | A non-stocked component moves no stock and creates no item; drafting flattens the tree | `expansion.expand_recipe_version` + `_write_lines` | `apps/kitchen/tests/test_production_contracts.py::TestOneSharedExpansion` · `apps/kitchen/tests/test_production_services.py::TestZeroInventoryAndZeroGeneralLedger` | 3.4 | | Done |
| RCP-080 | Flattened lines record their source component version and tree path | `source_version`, `component_path`, `component_label_path` on every requirement | `apps/kitchen/tests/test_production_services.py::TestTheDemoProductionDraft::test_the_draft_shows_every_shape_the_task_asks_for` · `apps/kitchen/tests/test_production_surface.py::TestScreensRender` | 3.4 | | Done |
| RCP-081 | Correcting a child is a new child version; existing parents are unaffected | versioning rule | — | 3.2 | | Specified |
| RCP-082 | No dish, cut, serving code or gram figure appears in `apps/kitchen` source | no dish or gram figure in code | `apps/kitchen/tests/test_draft_structure.py::TestServings` | 3.1 |  | Done |
| RCP-083 | A serving's unit converts to the output basis, once at entry | unit dimension check at entry | `apps/kitchen/tests/test_draft_structure.py::TestServings` | 3.1 |  | Done |
| RCP-084 | Exactly one primary serving per version | `recipe_serving_one_primary_per_version` | `apps/kitchen/tests/test_draft_structure.py::TestServings` | 3.1 |  | Done |
| RCP-085 | Rounding governs planning counts only and never moves money | rounding governs counts only | `apps/kitchen/tests/test_draft_structure.py::TestServings` | 3.3 |  | Done |
| RCP-086 | Cost per serving is a 6 dp rate over the serving's share of the output basis | `_serving_costs` | `apps/kitchen/tests/test_cost_servings.py::TestTheServingRate` | 3.3 | | Done |
| RCP-087a | A **standard recipe** cost splits across serving definitions by exact allocation, summing to the fils, at any count | `_compact_allocation`, derived from `apps/core/allocation.allocate` | `apps/kitchen/tests/test_cost_servings.py::TestTheServingAllocation` · `apps/kitchen/tests/test_cost_plate.py::TestTheCompactAllocationIsTheCertifiedOne` · `::TestServingAllocationHasNoSizeLimit` | 3.3 | Analytic and constant-size: 50,000 portions allocate exactly in one row | Done |
| RCP-087b | A **posted batch's actual** cost splits across the servings it really produced | — | — | 3.5 | Needs production, which does not exist yet. Split from RCP-087a on 2026-08-17 so the built half is not hidden behind the unbuilt one | Specified |
| RCP-088 | Servings never post: no stock, no journal, no source identity | servings post nothing | `apps/kitchen/tests/test_boundary_and_security.py::TestZeroLedgerEffect` | 3.1 |  | Done |
| RCP-089 | Servings imply a cost basis, never a price | no price field anywhere | `apps/kitchen/tests/test_recipe_master.py::TestNoMoneyAnywhere` | 3.1 |  | Done |
| RCP-090 | Both serving shapes are supported and neither is forced; the owner chooses per item | model neutrality | — | 3.1 | | Specified |
| RCP-091 | Phase 4's `MenuItem` binds to (Recipe, RecipeServing) from its own side | RecipeServing.public_id | `apps/kitchen/tests/test_draft_structure.py::TestServings` | 3.1 |  | Done |
| RCP-092 | The cost card splits by cost class, reproducing `KM-RCP-004`'s own summary | derived from allocated line values + a check constraint | `apps/kitchen/tests/test_costing.py::TestDirectLines::test_class_totals_add_up_to_the_document_total` · `apps/kitchen/tests/test_cost_demo.py::TestTheCostingScenario::test_the_food_and_packaging_totals_are_both_real` | 3.3 | | Done |
| RCP-093 | Cost ratio is a Phase 4 read, rendered "—" with its reason, never zero | `templates/kitchen/_cost_card.html` | `apps/kitchen/tests/test_cost_security.py::TestTheArabicSurface::test_the_cost_ratio_renders_as_a_dash_with_its_reason` | 3.3 | | Done |
| RCP-094 | A Release 1 batch satisfies seven enumerated conditions, each a test | one date, one warehouse, no partial completion, no in-progress state, one open period, atomic posting, inert draft | `apps/kitchen/tests/test_production_posting.py::TestRefusals` · `apps/kitchen/tests/test_production_posting.py::TestValueConservation` · `apps/kitchen/tests/test_production_posting_surface.py::TestAllocationsOverTheWire::test_a_partial_allocation_is_refused` | 3.5 | | Done |
| RCP-095 | Multi-day or partial production is refused with a named error, never approximated | service refusals | — | 3.5 | | Specified |
| RCP-096 | A draft batch holds nothing: no stock, reservation, valuation or reorder effect | no posting import anywhere in production code; readiness queries no stock | `apps/kitchen/tests/test_production_services.py::TestZeroInventoryAndZeroGeneralLedger` · `::TestReadiness::test_readiness_queries_no_stock_and_writes_nothing` | 3.4 | Census over nine tables, values as well as counts | Done |
| RCP-097 | A YES to KD-09 blocks Task 3.5 until WIP custody and accounting are specified | documented gate | — | 3.5 | | Specified |
| RCP-098 | Consumption is two distinct reads, never one | two report services | — | 3.8 | | Specified |
| RCP-099 | `consumed_quantity` is primary evidence; post-posting returns are Phase 1 documents plus a link | link model | — | 3.8 | | Specified |
| RCP-100 | Waste is linked to a batch, never inferred from date or item | link model | — | 3.8 | | Specified |
| RCP-101 | The link model is kitchen-owned and annotates only; inventory never imports kitchen | app boundary test | — | 3.8 | | Specified |
| RCP-102 | Attributed quantity never exceeds the source line | service under lock + verifier | — | 3.8 | | Specified |
| RCP-103 | Every posted movement falls in exactly one bucket; transfers are custody, not consumption | partition query | — | 3.8 | | Specified |
| RCP-104 | The partition's stock identity reconciles to the Phase 1 warehouse balance | verifier assertion | — | 3.9 | | Specified |
| RCP-105 | Waste is classified by what was lost; finished-output waste never joins ingredient consumption | report classification | — | 3.8 | | Specified |
| RCP-106 | Corrections stay corrections and are excluded from consumption | dedicated report column | — | 3.8 | | Specified |
| RCP-107 | No `is_kitchen` flag; the reader selects the warehouse | report scoping | — | 3.8 | | Specified |
| RCP-108 | The meal process is not financially complete, and every meal surface says so | screen, report and CSV copy | — | 3.7 | | Specified |
| RCP-109 | A serving divides one output; it is not a second output | model shape | — | 3.1 | | Specified |
| RCP-110 | Multi-output begins when two items receive stock from one input pool; deferred | documented boundary | — | 3.5 | | Specified |
| RCP-111 | A by-product cannot be activated by data alone; no posting path exists | absent code path | — | 3.5 | | Specified |
| RCP-112 | `verify_kitchen` also proves zero nets, no over-attribution, the partition, and no orphan links | the verifier | — | 3.9 | | Specified |
| RCP-113 | The legitimate no-journal case carries three explicit test obligations | all three shipped: no `JournalEntry` row; a netted balanced entry when accounts differ; full source identity on the stock entry in **both** cases | `apps/kitchen/tests/test_production_posting.py::TestTheJournalAndItsSilence` | 3.5 | | Done |
| RCP-114 | Worked examples use symbols or values labelled illustrative | documentation and fixtures | — | 3.1–3.10 | | Specified |
| RCP-115 | No Phase 3 surface calls any figure it can compute "profit"; margins are named | view layer and copy | — | 3.9 | | Specified |
| RCP-116 | Price minus material cost is not net profit, and the system says so where it would be assumed | report headings | — | 3.9 | | Specified |
| RCP-117 | The three sources map onto batch recipe, portion recipe and the approval instrument | documented mapping | — | 3.1 | | Specified |
| RCP-118 | Source documents populate `DRAFT` versions and steps; they approve nothing | importer status guard | — | 3.10 | | Specified |
| RCP-119 | Every imported row records `source_document` and `source_page` | SourceProvenance + constraint | `apps/kitchen/tests/test_recipe_master.py::TestProvenance` | 3.1, 3.10 |  | Done |
| RCP-120 | Every quantity carries a `measurement_basis`; no report compares across bases | MeasurementBasis on every quantity | `apps/kitchen/tests/test_draft_structure.py::TestLines` | 3.1, 3.8 |  | Done |
| RCP-121 | Conflicting source claims are both stored and surfaced, never silently reconciled | importer and conflict report | — | 3.10 | | Specified |
| RCP-122 | No PDF is a runtime dependency; none is tracked in Git | no PDF at runtime | `apps/kitchen/tests/test_boundary_and_security.py::TestNoImportsFromTheProprietarySources` | 3.1–3.11 |  | Done |
| RCP-123 | Servings convert the output; plates are separate approved compositions | serving model and portion recipes | — | 3.1, 3.3 | | Specified |
| RCP-124 | No code derives one plate's quantity, cost or price by doubling or halving another's | convention test and costing services | — | 3.3 | | Specified |
| RCP-125 | Task 3.10 is accepted only when all ten data-gate conditions hold | task exit criteria | — | 3.10 | | Specified |
| RCP-126 | Demo recipes stay fiction and carry no real dish name or sourced gram figure | DEMO banner on every row | `apps/kitchen/tests/test_demo_and_concurrency.py::TestDemoDataset` | 3.1–3.10 |  | Done |
| RCP-127 | The lifecycle is six states with one terminal state; no status depends on the current date and none is unreachable | `RecipeVersionStatus` + `is_expired_on` | `apps/kitchen/tests/test_draft_structure.py::TestTheLifecycleBoundary` | 3.2A |  | Done |
| RCP-128 | No global `CHEF` role; the four signatures are review types on one document | `RecipeReviewType` + `review_recipe_version` | `apps/kitchen/tests/test_boundary_and_security.py::TestPermissionMap` | 3.2A |  | Done |
| RCP-129 | The approver is never the author, the submitter or a reviewer; kitchen and costing reviews are two people | `approve_recipe_version` + `_require_distinct_parties` | `apps/kitchen/tests/test_version_lifecycle.py::TestApproval` | 3.2A |  | Done |
| RCP-130 | Organization-wide effective scope is materialised per branch, so overlap is enforceable | `RecipeVersionBranchScope` + `kitchen_scope_follows_its_version` | `apps/kitchen/tests/test_effective_dating.py::TestOverlapEnforcement` | 3.2A |  | Done |
| RCP-131 | The effective range is inclusive at both ends and expressed once; the supersession seam has no gap and no overlap | `covers_on_date` + `daterange(..., '[]')` | `apps/kitchen/tests/test_effective_dating.py::TestRangeBoundaries` | 3.2A |  | Done |
| RCP-132 | A child is validated as effective at the parent's **start date** on initial activation only; the frozen exact reference survives the child's later supersession, and nothing is re-pointed or cascaded | `require_effective_coverage` + `component_advisories` | `apps/kitchen/tests/test_component_coverage.py::TestSupersessionAfterActivation` | 3.2B |  | Done |
| RCP-133 | Two concurrent component additions cannot jointly close a cycle, because a draft is never a child | `create_recipe_component` draft-only rule + `lock_component_graph` | `apps/kitchen/tests/test_component_concurrency.py::TestTwoEdgesCannotCloseACycle` | 3.2B |  | Done |
| RCP-134 | The component multiplier is a technical identity at the repository's factor precision, rendered with a period | `RecipeComponent.multiplier_display` | `apps/kitchen/tests/test_components.py::TestTheMultiplier` | 3.2B |  | Done |

**Extended by Task 3.3 (2026-08-17) to RCP-138.** RCP-135 – RCP-138 record what
costing settled that the specification had not: the reproducible ledger cutoff,
the two unavailable valuation states, the leftover output that carries cost, and
the fingerprint that deliberately ignores its own answer.

| ID | Requirement | Where | Test | Task | Note | Status |
|---|---|---|---|---|---|---|
| RCP-135 | A cost is reproducible from one integer: the organization's posted-sequence high-water mark, captured once per calculation and stored on every snapshot | `apps/inventory/valuation.posted_cutoff` | `apps/kitchen/tests/test_cost_concurrency.py::TestARacingReceipt::test_every_line_of_one_card_reads_the_same_cutoff` | 3.3 | Why `POSTED_AS_OF` is the only authoritative basis: its movement set is a prefix, so it has a name | Done |
| RCP-136 | "Valued at zero" and "not valued" are different facts, and only the first is a cost | `ValuationState` | `apps/kitchen/tests/test_costing.py::TestTheValuationRead` | 3.3 | A positive quantity with no value is a real zero-cost position; an empty position has no average to divide | Done |
| RCP-137 | Output left over after whole servings carries cost and takes its own allocation weight | `_serving_costs` | `apps/kitchen/tests/test_cost_servings.py::TestTheServingAllocation::test_leftover_output_carries_cost_rather_than_vanishing` | 3.3 | Dropping it under-sums the scenario; absorbing it overstates one serving | Done |
| RCP-138 | A snapshot's idempotency fingerprint covers the request and never the resulting figures | `snapshots.snapshot_fingerprint` | `apps/kitchen/tests/test_cost_snapshots.py::TestIdempotency::test_the_fingerprint_ignores_the_figures_it_produced` | 3.3 | Hashing the answer would turn every honest re-run into a permanent conflict | Done |
| RCP-139 | Standard plate cost divides by the version's **primary** `RecipeServing`, and equals that serving's own standard rate exactly | `costing._plate_cost` | `apps/kitchen/tests/test_cost_plate.py::TestPlateCost::test_plate_cost_equals_the_primary_servings_own_rate` | 3.3 | No `portions_per_batch` column exists; adding one would be a second mutable statement of a fact the serving holds. RCP-084 guarantees exactly one primary | Done |
| RCP-140 | A serving allocation is stored compactly — two amounts, two counts, one leftover — and reconstructs the exact total at any serving count | `costing._compact_allocation` | `apps/kitchen/tests/test_cost_plate.py::TestTheCompactAllocationIsTheCertifiedOne` · `::TestServingAllocationHasNoSizeLimit::test_fifty_thousand_servings_still_allocate_exactly` | 3.3 | Equal-weight servings admit only two amounts, so the compact form **is** the distribution rather than a summary of it. Held against the certified allocator itself | Done |

## Phase 3 — Tasks 3.8 and 3.9 (accelerated implementation checkpoints)

Evidence here is a **development-database smoke or probe**, not a test, under the
owner's accelerated policy. `docs/runbooks/phase-3-deferred-verification.md` rows
18 – 32 record what Task 3.11 must still certify. Nothing below may be described
as suite-verified.

| Rule | Statement | Implementation | Evidence | Task | Status |
|---|---|---|---|---|---|
| RCP-103 | Every posted movement at a kitchen warehouse belongs to exactly one bucket | `consumption.classify_kitchen_movement` | smoke: 76 movements, 76 unique ids, 15 buckets populated | 3.8 | Implemented |
| RCP-104 | `closing − opening = Σ buckets` per `(warehouse, item, lot)` | `consumption.kitchen_warehouse_flow` | smoke: identity holds across 15 `(warehouse, item)` rows | 3.8 | Implemented |
| RCP-105 | Waste is classified by what was lost; output waste is not expanded into ingredients | `_DIRECT_BUCKETS` + `produced_output_item_ids` | smoke: `PRODUCED_OUTPUT_WASTE` = 1 on the demo waste of `DEMO-RICE-COOKED` | 3.8 | Implemented |
| RCP-106 | Count and adjustment corrections are excluded from consumption | `CORRECTION_BUCKETS` | smoke: corrections reported in their own columns | 3.8 | Implemented |
| RCP-098 | Consumption is two distinct reads, neither defined over the other's rows | `batch_actual_consumption` · `period_actual_consumption` | smoke: batch quantity and value equations both hold | 3.8 | Implemented |
| RCP-099 | Batch consumption is the posted evidence; §11.2's adjusted formula is superseded | ADR-026 §4 | `document_links` is not read by `batch_actual_consumption` | 3.8 | Amended |
| RCP-100 | Waste and returns are linked, never assumed | `BatchDocumentLink` | demo: 2 links, one of each closed type | 3.8 | Implemented |
| RCP-101 | The link model is kitchen-owned and one-directional | `apps/kitchen/document_links.py` | `apps.inventory` imports nothing from it | 3.8 | Implemented |
| RCP-102 | Attribution may not exceed the source line | service `FOR UPDATE` + migration 0023 trigger | probe: service refuses with `link_attribution_exceeds_source`; trigger refuses the same insert with the service bypassed | 3.8 | Implemented |
| RCP-047 | Theoretical consumption takes a quantity source; sales are Phase 4's contribution | `TheoreticalConsumptionSource` | smoke: `SALES` reports `DEFERRED_TO_PHASE_4`, `is_final` is `False` | 3.8 | Interface ready |
| RCP-043 | Meals explain fed-but-not-sold output rather than consuming stock again | `staff_meal_equivalent_usage` | smoke: 2 staff and 1 complimentary contributions, cancelled meal excluded | 3.8 | Implemented |
| RCP-044 | Meal accounting reclassification is deferred and recorded | `verify_kitchen` section 5 | reported as `MEAL_ACCOUNTING_RECLASSIFICATION_DEFERRED` | 3.8 | Deferred |
| — | A variance computed over part of the evidence discloses what it excluded | `consumption.PARTIALLY_COMPARABLE` | probe: 3 demo rows carry `DEMO-OIL 1.500000 L` outside a −1.75 KG variance | 3.8 | Implemented |
| RCP-050 | Verification reports and refuses to repair | `manage.py verify_kitchen` | run: 0 ERROR, exit 0, no `--fix` flag exists | 3.9 | Implemented |
| RCP-049 | `verify_kitchen` proves posting, value conservation, journals and Inventory-to-GL | composes seven verifiers | run: sections 1 – 10 all clean | 3.9 | Implemented |
| RCP-112 | The absent journal is absent for the right reason | `production_posting_reconciliation` recomputes per-account nets | composed by `verify_kitchen` section 4 | 3.9 | Composed |
| RCP-107 | No `is_kitchen` flag; the reader selects the warehouse | `FlowFilters.warehouse_id` | every consumption screen scopes by warehouse | 3.8 | Implemented |


## Phase 4 — Sales and Settlements

Specified by Task 4.0 (`docs/tasks/task-4-0-sales-domain-spec.md`, ADR-027,
ADR-028) and delivered across checkpoints 4.1 to 4.7. The invariants these rows
cite are numbered in `docs/invariants/sales-invariants.md`; the journals are
written out in `docs/development/sales-accounting.md`.

Two conventions carried from Phase 3. A row may cite a **constraint or a
trigger** instead of a test, because an `EXCLUDE USING gist` *is* the
enforcement and pointing at a test that merely observes it would be the weaker
claim. And a row is `Done` only when the evidence exists — every citation below
resolves to a test that runs.

| Req ID | Summary | Module / service | Tests | Task | Status |
|---|---|---|---|---|---|
| SLS-001 | A menu item names a recipe and a serving that some version of it offers; `DIRECT_STOCK` is refused | `apps/sales/services.create_menu_item` | `apps/sales/tests/test_menu_and_channels.py` · `apps/sales/tests/test_verify_sales.py::test_a_menu_item_whose_serving_lapsed_is_an_error` | 4.1 | Done |
| SLS-002 | Two active prices never overlap within one scope; resolution is most specific wins | `apps/sales/selectors.effective_prices`, `EXCLUDE USING gist` in migration `0002` | `apps/sales/tests/test_menu_and_channels.py` | 4.1 | Done |
| SLS-003 | An application channel settles into a receivable and never into a drawer | `SalesChannel` check constraints | `apps/sales/tests/test_menu_and_channels.py` | 4.1 | Done |
| SLS-004 | A discount programme's two funding shares add to exactly one hundred, and an application-funded share names its application | `CheckConstraint` + `services.create_discount_program` | `apps/sales/tests/test_agreements_and_discounts.py` · `apps/sales/tests/test_verify_sales.py::test_a_discount_whose_funding_does_not_close_is_refused` | 4.2 | Done |
| SLS-005 | Four commission bases, each computed on its own stated amount, and every field the commission used is frozen on the line | `apps/sales/agreements.py` | `apps/sales/tests/test_agreements_and_discounts.py` | 4.2 | Done |
| SLS-006 | Every resolved identity is stored on the line, never re-derivable (ADR-024) | `apps/sales/day_services.resolve_line` | `apps/sales/tests/test_daily_sales_posting.py` | 4.3 | Done |
| SLS-007 | Revenue is credited **gross**; the restaurant-funded discount is a separate debit beside it | `apps/sales/posting.build_plan` | `apps/sales/tests/test_daily_sales_posting.py` · `apps/sales/tests/test_verify_sales.py::test_revenue_that_stops_being_gross_is_an_error` | 4.3 | Done |
| SLS-008 | The **application-funded** discount reaches no account at all | `build_plan` omits it | `apps/sales/tests/test_daily_sales_posting.py` · `apps/sales/tests/test_sales_dashboard.py::test_the_application_funded_discount_is_shown_and_never_subtracted` | 4.3 | Done |
| SLS-009 | The application receivable is append-only; there is no balance field anywhere in the module | `sales_receivable_is_append_only` trigger | `apps/sales/tests/test_verify_sales.py::test_the_receivable_ledger_refuses_a_raw_update` | 4.3 | Done |
| SLS-010 | A posted day is frozen by a whole-row allowlist trigger; correction is reversal or adjustment | migration `0006` | `apps/sales/tests/test_daily_sales_posting.py` · `apps/sales/tests/test_verify_sales.py::test_a_posted_sales_line_refuses_a_raw_update` | 4.3 | Done |
| SLS-011 | An adjustment posts to `SALES_RETURNS` and never touches `SALES_REVENUE` | `apps/sales/adjustment_posting.build_adjustment_plan` | `apps/sales/tests/test_sales_adjustments.py` · `apps/sales/tests/test_verify_sales.py::test_an_adjustment_that_reaches_revenue_is_an_error` | 4.4 | Done |
| SLS-012 | No adjustment takes back more quantity or gross than its original line carried | `sales_adjustment_line_is_within_its_original` trigger | `apps/sales/tests/test_sales_adjustments.py` · `apps/sales/tests/test_verify_sales.py::test_a_posted_adjustment_line_refuses_a_raw_update` | 4.4 | Done |
| SLS-013 | A `FINANCIAL_CORRECTION` moves money and no quantity | the same trigger | `apps/sales/tests/test_sales_adjustments.py` · `apps/sales/tests/test_sales_demo_seed.py::test_the_financial_correction_takes_back_money_and_no_quantity` | 4.4 | Done |
| SLS-014 | **Only** `CANCELLED_BEFORE_FULFILLMENT` reduces theoretical consumption | `apps/sales/consumption_source.cancelled_quantities` | `apps/sales/tests/test_sales_adjustments.py` · `apps/sales/tests/test_verify_sales.py::test_only_a_cancellation_reduces_the_theoretical_quantity` | 4.4 | Done |
| SLS-015 | Expected, statement and remitted are kept as three figures; every dinar of each gap is claimed | `apps/sales/settlement_services.three_way_for` | `apps/sales/tests/test_application_settlements.py` · `apps/sales/tests/test_sales_api.py::test_the_settlement_payload_keeps_the_three_figures_apart` | 4.5 | Done |
| SLS-016 | An unexplained gap on either leg blocks reconciliation exactly, with no tolerance | `reconcile_settlement`, `unexplained_variance` | `apps/sales/tests/test_application_settlements.py` | 4.5 | Done |
| SLS-017 | `UNEXPLAINED_APPROVED` costs a written explanation and a named approver | two `CheckConstraint`s in migration `0009` | `apps/sales/tests/test_application_settlements.py` | 4.5 | Done |
| SLS-018 | A settlement journal contains **no** class-6 line: commission is recognised once, at the sale | `apps/sales/settlement_posting.build_settlement_plan` | `apps/sales/tests/test_application_settlements.py` · `apps/sales/tests/test_verify_sales.py::test_a_settlement_that_debits_commission_twice_is_an_error` | 4.5 | Done |
| SLS-019 | No receivable entry is allocated beyond what it owes, across posted settlements | `sales_settlement_allocation_is_within_its_entry` trigger | `apps/sales/tests/test_application_settlements.py` | 4.5 | Done |
| SLS-020 | The approver of a cashier closing is never its closer — in the service and at the database | `sales_shift_approver_is_not_the_closer` + `approve_cashier_shift` | `apps/sales/tests/test_cashier_shifts.py` · `apps/sales/tests/test_sales_api.py::test_approving_your_own_closing_is_a_409` | 4.6 | Done |
| SLS-021 | A closing posts the approved cash over/short variance and nothing else | `apps/sales/shift_posting.build_shift_plan` | `apps/sales/tests/test_cashier_shifts.py` · `apps/sales/tests/test_verify_sales.py::test_a_shift_journal_touching_the_wrong_accounts_is_an_error` | 4.6 | Done |
| SLS-022 | A zero variance posts no journal at all and still takes a number | `approve_cashier_shift` | `apps/sales/tests/test_cashier_shifts.py` | 4.6 | Done |
| SLS-023 | A shift closes only against a `POSTED` day | `close_cashier_shift`, `day_not_posted` | `apps/sales/tests/test_cashier_shifts.py` | 4.6 | Done |
| SLS-024 | The daily reconciliation stores nothing and offers no acknowledge control | `apps/sales/daily_reconciliation.py`, `report_views.py` | `apps/sales/tests/test_cashier_shifts.py` | 4.6 | Done |
| SLS-025 | `net_revenue` is the ledger's own arithmetic and can be found in the general ledger | `apps/sales/dashboard.headline_for` | `apps/sales/tests/test_sales_dashboard.py::test_net_revenue_is_the_ledgers_own_arithmetic` | 4.7 | Done |
| SLS-026 | No `float` appears in a dashboard aggregate; shares are exact `Decimal`s quantized once | `apps/sales/dashboard.py` | `apps/sales/tests/test_sales_dashboard.py::test_mixes_and_top_items_share_to_a_hundred` | 4.7 | Done |
| SLS-027 | Shortage and overage are reported separately and never netted | `dashboard.cashier_summary` | `apps/sales/tests/test_sales_dashboard.py::test_shortage_and_overage_are_never_netted` | 4.7 | Done |
| SLS-028 | A line with no cost snapshot behind it is counted and reported, never costed at zero | `dashboard.cost_summary` | `apps/sales/tests/test_sales_dashboard.py::test_cost_summary_never_values_a_line_at_zero` | 4.7 | Done |
| SLS-029 | Cost and margin are **omitted, never blanked**, without `view_sales_cost` | the card registry + `/dashboard/cost` | `apps/sales/tests/test_sales_dashboard.py::test_a_manager_without_cost_gets_no_cost_card_and_a_403_on_its_route` · `apps/sales/tests/test_sales_api.py::test_cost_is_a_separate_route_and_carries_no_key_on_the_dashboard` | 4.7 | Done |
| SLS-030 | Every dashboard card answers as its own htmx fragment with no second shell | `apps/sales/dashboard_views.SalesDashboardCardView` | `apps/sales/tests/test_sales_dashboard.py::test_every_card_route_answers_as_a_fragment` | 4.7 | Done |
| SLS-031 | Money, quantities and rates cross the API as exact strings, both directions | `apps/sales/api.py` | `apps/sales/tests/test_sales_api.py::test_every_read_answers_and_carries_no_json_number` · `apps/sales/tests/test_sales_api.py::test_the_day_payload_reconstructs_its_own_totals` | 4.7 | Done |
| SLS-032 | Out of scope is 404 and never 403, including for a malformed identifier | `apps/sales/selectors.py` + `api._by_public_id` | `apps/sales/tests/test_sales_api.py::test_an_unknown_document_is_a_404_and_not_a_403` | 4.7 | Done |
| SLS-033 | No `PATCH` and no `DELETE` on anything that has left `DRAFT` or `OPEN` | the router's shape | `apps/sales/tests/test_sales_api.py::test_a_posted_day_offers_no_patch_and_no_delete` | 4.7 | Done |
| SLS-034 | A cashier may draft and submit a day and may not post it | `require_branch_permission` per transition | `apps/sales/tests/test_sales_api.py::test_a_cashier_may_draft_a_day_and_may_not_post_it` | 4.7 | Done |
| SLS-035 | A state conflict answers 409 and a fixable input answers 422 | `CONFLICT_CODES` in `config/api.py` | `apps/sales/tests/test_sales_api.py::test_posting_a_posted_day_is_a_409_and_not_a_422` · `apps/sales/tests/test_sales_api.py::test_a_malformed_decimal_is_a_422_naming_the_field` | 4.7 | Done |
| SLS-036 | The Sales router is registered under the versioned prefix | `config/api.py` | `apps/sales/tests/test_sales_api.py::test_the_router_is_registered_under_the_versioned_prefix` | 4.7 | Done |
| SLS-037 | `verify_sales` reports and refuses to repair; there is no `--fix` | `apps/sales/management/commands/verify_sales.py` | `apps/sales/tests/test_verify_sales.py::test_the_command_offers_no_repair_flag` | 4.7 | Done |
| SLS-038 | A clean module exits zero with advisories present; an error exits non-zero | the command's epilogue | `apps/sales/tests/test_verify_sales.py::test_the_command_runs_and_exits_zero_on_a_clean_module` · `apps/sales/tests/test_verify_sales.py::test_the_command_exits_non_zero_on_an_error` | 4.7 | Done |
| SLS-039 | A commission gap with a delivery company is an ADVISORY and never an ERROR | `reconciliation.verify_settlement_commission` | `apps/sales/tests/test_verify_sales.py::test_the_commission_gap_is_an_advisory_and_never_an_error` | 4.7 | Done |
| SLS-040 | A drawer counted but not yet approved is a COVERAGE_LIMITATION | `reconciliation.verify_shift_counts` | `apps/sales/tests/test_verify_sales.py::test_a_shift_closed_but_not_yet_approved_is_a_coverage_limitation` | 4.7 | Done |
| SLS-041 | The receivable subledger is compared per **control account**, not per application | `reconciliation.verify_receivable_ledger` | `apps/sales/tests/test_verify_sales.py::test_an_appended_receivable_entry_breaks_the_subledger` | 4.7 | Done |
| SLS-042 | Every sales journal carries a complete, upper-case source identity and names a real document | `reconciliation.verify_source_identity` | `apps/sales/tests/test_verify_sales.py::test_a_journal_with_no_document_behind_it_is_an_error` | 4.7 | Done |
| SLS-043 | Seventeen permissions: every declared name granted, every grant scoped, every grant migrated | `apps/sales/permissions.py` | `apps/sales/tests/test_verify_sales.py::test_the_permission_table_is_complete_at_seventeen` · `apps/sales/tests/test_verify_sales.py::test_every_declared_permission_name_is_granted_and_migrated` | 4.7 | Done |
| SLS-044 | With the `SALES` adapter registered, no surface reports `SALES_NOT_INCLUDED_PHASE_4` | `kitchen.consumption_sources.coverage_code` | `apps/sales/tests/test_verify_sales.py::test_the_sales_quantity_source_is_registered` | 4.7 | Done |
| SLS-045 | `seed_sales_demo` refuses outside `DEBUG` and posts nothing without `--confirm-demo` | the command's guards | `apps/sales/tests/test_sales_demo_seed.py::test_it_refuses_to_run_outside_debug` · `apps/sales/tests/test_sales_demo_seed.py::test_it_posts_nothing_without_confirm_demo` | 4.7 | Done |
| SLS-046 | A second seed run creates nothing: every table counted before and after, journals included | `apps/sales/demo.py` | `apps/sales/tests/test_sales_demo_seed.py::test_a_second_run_creates_nothing_at_all` | 4.7 | Done |
| SLS-047 | The demo posts through the real services; no journal is written by hand | `apps/sales/demo.py` | `apps/sales/tests/test_sales_demo_seed.py::test_the_seed_writes_no_journal_by_hand` | 4.7 | Done |
| SLS-048 | Demo delivery applications are fictional and every record says it is a demo | `DEMO_BANNER`, `DEMO_NAMESPACE` | `apps/sales/tests/test_sales_demo_seed.py::test_the_delivery_applications_are_fictional` · `apps/sales/tests/test_sales_demo_seed.py::test_every_record_says_it_is_a_demo` | 4.7 | Done |
| SLS-049 | The demo's business dates are fixed, so a second run is a retry rather than a new day | `demo.ANCHOR` | `apps/sales/tests/test_sales_demo_seed.py::test_the_business_dates_are_fixed_and_not_todays` | 4.7 | Done |
| SLS-050 | The seeded module verifies clean, which is what makes the demo worth having | `seed_sales_demo` + `verify_sales` | `apps/sales/tests/test_sales_demo_seed.py::test_the_seeded_module_verifies_clean` | 4.7 | Done |
| SLS-051 | Twelve navigation sections, all active, no قريباً badge left in Sales | `apps/core/navigation.py` | `apps/core/tests/test_shell.py::test_a_finished_module_has_no_inert_section_left` | 4.7 | Done |
| SLS-052 | Task 4.0's seventeen chart accounts are in the Phase 0 exit gate's count | `seed_chart_of_accounts.CHART` | `tests/test_phase_0_exit.py::TestTheFoundationsCooperate::test_the_whole_path_from_an_empty_database_to_a_closed_period` | 4.0 | Done |

## Phase 5 — Accounting

Specified by Task 5.0 (`docs/tasks/task-5-0-accounting-domain-spec.md`,
ADR-029, ADR-030, ADR-031) and delivered across checkpoints 5.1 to 5.13. The
invariants these rows cite are numbered in
`docs/invariants/accounting-module-invariants.md`; the documents are written
out in `docs/development/accounting-operations.md` and the reads in
`docs/development/accounting-reports.md`.

The Phase 3 and Phase 4 conventions carry over. A row may cite a **constraint,
a trigger or a management command** instead of a test, because the enforcement
*is* the constraint and pointing at a test that merely observes it would be the
weaker claim. A row is `Done` only when the evidence exists and runs.

| Req ID | Summary | Module / service | Tests | Task | Status |
|---|---|---|---|---|---|
| ACT-001 | The Phase 0 kernel stays the only ledger; Phase 5 adds no second journal store | no journal model outside `apps/accounting/models.JournalEntry` | `apps/accounting/tests/test_verify_accounting.py::test_a_clean_organization_reports_no_blocking_findings` | 5.1 | Done |
| ACT-002 | No mutable balance column exists on any accounting model | `reconciliation.verify_no_stored_balance` | `apps/accounting/tests/test_api_reports.py::test_cashbox_endpoint_returns_no_balance` · `apps/accounting/tests/test_verify_accounting.py::test_no_accounting_model_carries_a_stored_balance` | 5.1 | Done |
| ACT-003 | Every balance is derived from POSTED and REVERSED journal lines at read time | `selectors.account_balance`, `reports._scoped_lines` | `apps/accounting/tests/test_api_reports.py::test_trial_balance_ties_and_reports_strings` | 5.8 | Done |
| ACT-004 | A manual journal's creator may not post it; system journals are exempt | `services.validate_manual_maker_checker` | `apps/accounting/tests/test_api.py::test_create_amend_post` | 5.2 | Done |
| ACT-005 | A RESTRICTED account refuses a hand-written line without the override permission | `services.validate_manual_posting_policy` | `apps/accounting/tests/test_posting.py` | 5.2 | Done |
| ACT-006 | A posted journal is frozen by a whole-row allowlist trigger, not a blocklist | migration `accounting/0005` | `apps/accounting/tests/test_hardening.py` | 0.6 | Done |
| ACT-007 | Role mappings are effective-dated and versioned; a used mapping is closed, never rewritten | `commands.close_account_role_mapping` | `apps/accounting/tests/test_account_mappings.py` | 5.1 | Done |
| ACT-008 | No account code is written into any accounting service; every account is resolved by role | `services.resolve_default_account` | `apps/accounting/tests/test_report_mapping.py` | 5.1 | Done |
| ACT-009 | One GL account carries at most one active cash record | partial unique index on `(organization, account) WHERE is_active` | `apps/accounting/tests/test_cash_accounts.py` | 5.3 | Done |
| ACT-010 | Bank account numbers are stored masked | `cash_services._mask` | `apps/accounting/tests/test_cash_accounts.py` | 5.3 | Done |
| ACT-011 | Reconciliation stamps a date and changes no figure, because none is stored | `cash_services.record_reconciliation` | `apps/accounting/tests/test_cash_accounts.py` | 5.3 | Done |
| ACT-012 | The supplier workspace forwards to Procurement's `supplier_aging` and derives nothing | `report_commands.read_supplier_liabilities` | `apps/accounting/tests/test_verify_accounting.py::test_it_forwards_the_other_modules_verifiers` | 5.4 | Done |
| ACT-013 | The application workspace forwards to Sales' `positions_for` and derives nothing | `report_commands.read_application_receivables` | `apps/accounting/tests/test_verify_accounting.py::test_it_forwards_the_other_modules_verifiers` | 5.4 | Done |
| ACT-014 | The subledger comparison uses `net_position`, not `open_total`, so standing credit ties | `reconciliation.verify_supplier_subledger` | `apps/accounting/tests/test_verify_accounting.py::test_a_clean_organization_reports_no_blocking_findings` | 5.4 | Done |
| ACT-015 | A subledger discrepancy is reported and never repaired automatically | no repair path in `reconciliation.py` | `apps/accounting/tests/test_verify_accounting.py::test_nothing_in_reconciliation_writes` | 5.4 | Done |
| ACT-016 | An expense voucher has no supplier field and no tax field | `ExpenseVoucher` model | `apps/accounting/tests/test_api_documents.py::test_expense_voucher_full_lifecycle` | 5.5 | Done |
| ACT-017 | A voucher is paid from exactly one source, cashbox or bank | `expense_voucher_exactly_one_payment_source` check constraint + `open_expense_voucher` | `apps/accounting/tests/test_api_documents.py::test_voucher_needs_exactly_one_payment_source` | 5.5 | Done |
| ACT-018 | The voucher's author may neither approve nor post it, whatever permissions they hold | `expense_services.approve_expense_voucher` / `post_expense_voucher` | `apps/accounting/tests/test_api_documents.py::test_author_cannot_approve_own_voucher` | 5.5 | Done |
| ACT-019 | A voucher total is the sum of its lines, never rounded independently | `expense_services.recompute_total` | `apps/accounting/tests/test_api_documents.py::test_expense_voucher_full_lifecycle` | 5.5 | Done |
| ACT-020 | Nothing that reached the ledger takes the discard path | `expense_services.discard_expense_voucher` | `apps/accounting/tests/test_api_documents.py::test_posted_voucher_cannot_be_discarded` | 5.5 | Done |
| ACT-021 | An accrual line names an expense account and never revenue, liability or equity | `deferral_services.add_accrual_line` | `apps/accounting/tests/test_api_documents.py::test_accrual_line_refuses_a_non_expense_account` | 5.6 | Done |
| ACT-022 | Reversing an accrual may record which invoice superseded it; Accounting never creates that invoice | `deferral_services.reverse_accrual` | `apps/accounting/tests/test_api_documents.py::test_accrual_lifecycle_and_line_totals` | 5.6 | Done |
| ACT-023 | A prepayment schedule is split by the certified allocator and sums to its header exactly | `deferral_services.build_schedule` + `apps/core/allocation.py` | `apps/accounting/tests/test_api_documents.py::test_prepayment_schedule_is_exact` | 5.6 | Done |
| ACT-024 | `end_date` is derived from the last schedule period and never submitted | `deferral_services.open_prepayment` | `apps/accounting/tests/test_api_documents.py::test_prepayment_end_date_cannot_be_submitted` | 5.6 | Done |
| ACT-025 | An instalment into a closed period is refused, and the refusal names the period | `deferral_services.post_schedule_line` | `apps/accounting/tests/test_posting.py` | 5.6 | Done |
| ACT-026 | The pre-close report collects every blocker instead of raising on the first | `period_services.period_close_blockers` | `apps/accounting/tests/test_api_reports.py::test_pre_close_collects_every_blocker_not_just_the_first` | 5.7 | Done |
| ACT-027 | A close guard that raises unexpectedly becomes an advisory finding, not an aborted preview | `period_services._domain_guards` | `apps/accounting/tests/test_api_reports.py::test_pre_close_collects_every_blocker_not_just_the_first` | 5.7 | Done |
| ACT-028 | Statement order is one tuple, named once, and the running balance is accumulated in the service | `apps/accounting/statements.py` + `reports.general_ledger` | `apps/accounting/tests/test_api_reports.py::test_general_ledger_source_document_id_is_a_string` | 5.8 | Done |
| ACT-029 | `source_type` is upper-cased on the way in, matching stored source identity | `report_commands.read_general_ledger` | `apps/accounting/tests/test_source_identity.py` | 5.8 | Done |
| ACT-030 | The statements resolve their account set from the ledger, so an unmapped balance cannot vanish | `reports.classify` | `apps/accounting/tests/test_api_reports.py::test_unmapped_balance_is_shown_and_blocks_approval` | 5.9 | Done |
| ACT-031 | An unmapped account with a balance blocks approval and appears in غير مصنّف | `IncomeStatement.is_approvable`, `BalanceSheet.is_approvable` | `apps/accounting/tests/test_api_reports.py::test_unmapped_balance_is_shown_and_blocks_approval` | 5.9 | Done |
| ACT-032 | Current-year earnings are computed, so the equation holds without monthly closing entries | `reports.balance_sheet` | `apps/accounting/tests/test_api_reports.py::test_balance_sheet_reports_its_difference_rather_than_hiding_it` | 5.9 | Done |
| ACT-033 | An unbalanced statement reports its difference and offers no repair | `BalanceSheet.difference`; no repair path in `report_views.py` or `api_reports.py` | `apps/accounting/tests/test_api_reports.py::test_balance_sheet_reports_its_difference_rather_than_hiding_it` | 5.9 | Done |
| ACT-034 | Every CSV export calls the same service the screen did, with a BOM and neutralised cells | `report_views.py` + `apps/inventory/report_views.neutralise` | `apps/accounting/tests/test_accounting_ui.py::test_every_report_export_is_csv_with_a_bom` · `apps/accounting/tests/test_accounting_ui.py::test_an_exported_cell_cannot_execute_as_a_formula` | 5.8 | Done |
| ACT-035 | Every dashboard card answers as its own fragment, so one failure cannot blank the page | `apps/accounting/dashboard_views.py` | `apps/accounting/tests/test_accounting_ui.py::test_every_dashboard_card_answers_as_a_fragment` | 5.10 | Done |
| ACT-036 | Money crosses the API as a string in both directions | `api_documents.py`, `api_reports.py` | `apps/accounting/tests/test_api_documents.py::test_amounts_leave_as_strings` | 5.11 | Done |
| ACT-037 | `source_document_id` crosses the API as a string, because upstream ids are UUIDs as often as integers | `api_reports.LedgerRowOut` | `apps/accounting/tests/test_api_reports.py::test_general_ledger_source_document_id_is_a_string` | 5.11 | Done |
| ACT-038 | Out of scope is 404; in scope without authority is 403 | `document_commands.py`, `report_commands.py` | `apps/accounting/tests/test_api_documents.py::test_out_of_scope_voucher_is_404_not_403` · `::test_in_scope_without_authority_is_403` | 5.11 | Done |
| ACT-039 | Every new endpoint requires authentication, because the API authenticates by default | `config/api.py` `auth=django_auth` | `apps/accounting/tests/test_api_documents.py::test_anonymous_is_refused` | 5.11 | Done |
| ACT-040 | The API reaches reports under the same authority the screens use, neither wider nor narrower | `report_commands` uses `require_reachable_organization_permission` | `apps/accounting/tests/test_api_reports.py::test_reader_without_ledger_authority_cannot_read_reports` | 5.11 | Done |
| ACT-041 | Twenty-six accounting permissions: every declared name granted, every grant scoped and migrated | `apps/accounting/permissions.py` | `apps/accounting/tests/test_permissions.py` | 5.1 | Done |
| ACT-042 | `verify_accounting` reports and refuses to repair; there is no `--fix` | `apps/accounting/management/commands/verify_accounting.py` | `apps/accounting/tests/test_verify_accounting.py::test_the_command_offers_no_repair_flag` | 5.10 | Done |
| ACT-043 | The verifier composes Procurement's and Sales' own verifiers rather than re-deriving them | `reconciliation.verify_supplier_subledger` / `verify_application_subledger` | `apps/accounting/tests/test_verify_accounting.py::test_it_forwards_the_other_modules_verifiers` | 5.10 | Done |
| ACT-044 | A check that raises becomes a finding rather than aborting the run | `verify_accounting.Command.handle` | `apps/accounting/tests/test_verify_accounting.py::test_a_check_that_raises_becomes_a_finding` | 5.10 | Done |
| ACT-045 | The seeded demo verifies clean: 0 ERROR, 0 ADVISORY, 0 COVERAGE_LIMITATION | `seed_accounting_demo` + `verify_accounting` | `verify_accounting --organization DEMO-KHAN-MANDI` exits zero: 0 ERROR, 0 ADVISORY, 0 COVERAGE_LIMITATION | 5.10 | Verified |
| ACT-046 | The demo classifies every postable account, because an unmapped balance blocks the statements | `demo.seed_report_mappings` | the first verifier run reported 27 unclassified balances; the seed closed them | 5.10 | Verified |
| ACT-047 | Fifteen navigation sections, all active, no قريباً badge left in Accounting | `apps/core/navigation.py` | `apps/core/tests/test_shell.py::test_a_finished_module_has_no_inert_section_left` | 5.10 | Done |
| ACT-048 | IQD only: no currency field, no rate table, no tax field anywhere in the module | the absence across `apps/accounting/models.py` | `apps/accounting/tests/test_commit_boundary.py` | 5.0 | Done |
