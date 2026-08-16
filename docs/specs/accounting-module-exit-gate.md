# Accounting module exit gate

The check the Accounting module passes before it is called complete, in the
same shape as the Phase 2 procurement exit gate
(`docs/tasks/phase-2-task-breakdown.md` §2.18).

It existed as a demand and not a definition until now: `overnight-progress.md`
named an "Accounting-side module-exit check" three times and no document said
what it consisted of. An undefined gate cannot be passed or failed — it can
only be skipped or improvised, which is the same thing done twice.

## What is in scope

The Accounting module is **three approved tasks**: 0.6 (the journal kernel),
0.7 (permissions, admin, API, idempotent integration) and 0.8 (completion and
the Phase 0 exit gate). Task 1.3 later added the account-role and mapping
surface to the same app, so it is verified here too.

**What is not in scope, and why.** Trial balance, general ledger, profit and
loss, balance sheet, cashboxes, bank accounts, accruals, receivables and the
thirteen inert navigation sections are **Phase 5** in
`docs/architecture/architecture-charter.md`. No Phase 5 task ID exists in this
repository, and phases 3 and 4 have not started. A gate that demanded them
would be demanding work the plan has not reached; a gate that quietly counted
their absence as failure would never pass. They are listed here so their
absence is a recorded decision rather than an oversight.

## The checks

| # | Check | Passes when |
|---|---|---|
| 1 | **Task status** | 0.6, 0.7 and 0.8 are each DONE on code and test evidence, not on a document's claim; every deferral names a document and a reason |
| 2 | **Traceability** | Every approved task has a section or declared coverage — enforced by `tests/test_traceability.py::test_every_approved_task_is_represented_in_traceability` — and every `Done` row cites a test that exists |
| 3 | **ADR consistency** | No accepted accounting ADR describes built work as unbuilt; no ADR contradicts itself or the shipped behaviour; the decisions index agrees with the decisions |
| 4 | **Permissions** | Thirteen named permissions, each with a declared scope; roles carry them through groups; a service never checks a role name |
| 5 | **Journals** | Balanced on stored values, refused unbalanced by the database at COMMIT; only detail accounts; one organization per entry; gapless numbering per organization and year |
| 6 | **Reversal** | Correction is an exact mirror, once only, with a reason, original preserved |
| 7 | **Periods** | Resolved from the accounting date; CLOSED refuses; SOFT_CLOSED refuses routine postings but admits adjustments and reversals; closing and reopening are ordered |
| 8 | **Mappings** | Effective-dated, resolved never guessed; used mappings are closed rather than edited; create, amend, close and archive each have a screen guarded by permission **and** organization scope |
| 9 | **Source identity** | Complete or absent; `SourceEvent` is a closed enum; one economic event yields at most one journal |
| 10 | **Idempotency** | Keys unique per organization, matched against a request fingerprint; a reuse with a changed payload is a conflict, not a retry |
| 11 | **Admin lockdown** | Every accounting model is read-only in Django admin, superusers included |
| 12 | **Arabic RTL surfaces** | The approved screens — role list, mapping list, create, amend, close, archive — render RTL inside the shell, with CSS logical properties and no third-party requests |
| 13 | **Fresh database** | A new PostgreSQL database migrates from zero, seeds the chart and roles, and answers the reconciliation |
| 14 | **Reconciliation** | `verify_inventory_against_gl` and `verify_procurement_accounting` report zero discrepancies against the kernel's own derivations |
| 15 | **Definitive suite** | The complete project suite exits 0 on the final committed tree, with no competing pytest process |
| 16 | **Quality gates** | ruff, ruff format, mypy, `manage.py check`, `makemigrations --check`, pre-commit — all clean |
| 17 | **Git state** | Working tree clean, branch pushed, `main` untouched, the completion tag pushed |

## Exit

Tag `accounting-module-complete`, pushed. Not merged into `main`.

---

## Execution — 2026-08-15

Run on `khan_mandi_acct_gate`, a PostgreSQL database created empty and
migrated from zero for this gate.

| # | Check | Result |
|---|---|---|
| 1 | Task status | 0.6, 0.7, 0.8 DONE on code and test evidence; five deferrals each cite a document and a reason (audited by a seven-agent sweep whose critic re-ran the suite independently) |
| 2 | Traceability | Task 0.6 section added with `ACC-001`–`ACC-020`; the new coverage check parses 38 approved tasks and reports none missing; `test_traceability.py` 4 passed |
| 3 | ADR consistency | ADR-013/014/015 corrected; their settled questions answered; the decisions index reconciled; ADR-023 §5's self-contradiction removed in all three places it survived |
| 4 | Permissions | **13 declared, 13 scoped, all present in `PERMISSION_SCOPE`** |
| 5 | Journals | Chart seeds **77 accounts, 6 cost centres**; balance, line shape, postable-account and isolation rules held by `test_posting.py` (ACC-001 – ACC-006) |
| 6 | Reversal | `TestReversal`, `TestReversalErrorAccuracy` (ACC-009) |
| 7 | Periods | **12 periods, no thirteenth**; `TestPeriods`, `TestPeriodOrdering`, `TestSoftClosedAuthorization` (ACC-007) |
| 8 | Mappings | **17 system roles seeded**; create, amend, close and archive all have screens, all guarded by permission and scope — 17 tests in `test_mapping_views.py` (ACC-018, ACC-019) |
| 9 | Source identity | `SourceEvent` closed to exactly `['POSTED', 'REVERSED']` (ACC-012, IDM-002) |
| 10 | Idempotency | Per-organization keys with fingerprint matching; `test_idempotency.py` (ACC-012, IDM-001–008) |
| 11 | Admin lockdown | Journal entry and account **add** both answer **403** on the fresh database |
| 12 | Arabic RTL surfaces | `role_list`, `mapping_list`, `mapping_create` each **200 and `dir="rtl"`** |
| 13 | Fresh database | Migrated from zero, chart and roles seeded, screens answered |
| 14 | Reconciliation | Zero discrepancies from `verify_procurement_accounting` and `verify_inventory_against_gl` on `khan_mandi_p2_gate2`, the database carrying real posted documents; the payable check now reads the kernel's own `account_balance` |
| 15 | Definitive suite | See the runbook's Accounting exit entry |
| 16 | Quality gates | See the same entry |
| 17 | Git state | See the same entry |
