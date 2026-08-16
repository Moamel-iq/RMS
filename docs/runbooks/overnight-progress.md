# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: **Phase 3 opened — Task 3.0 specification authored,
awaiting human approval.** Branch `phase/3-kitchen` from the Phase 2 head.
No implementation begins until the spec is approved; Task 3.1 is gated on
that approval, not on any technical prerequisite.

Deliverables on the branch: `docs/tasks/task-3-0-recipes-production-domain-spec.md`
(RCP-001..RCP-057), `docs/tasks/phase-3-task-breakdown.md` (3.0–3.11, exit
tag `phase-3-kitchen-complete`), `docs/invariants/kitchen-invariants.md`
(30 proposed invariants), the Phase 3 traceability section (57 Specified
rows), and the coverage validator extended to the Phase 3 breakdown — all
twelve new task numbers verified covered.

The design decisions a reviewer should look at hardest, each argued in the
spec: Phase 3 adds exactly ONE stock-moving document (the batch) and reuses
every Phase 1 kitchen flow; value is conserved through a batch with yield
absorbed into unit cost (no standard cost, no variance journal); the batch
journal is a per-account net and is legitimately ABSENT when accounts net to
zero; NO new account roles, no WIP account, no new AccountRoleDomain; staff
and complimentary meals are memo documents feeding the theoretical side,
with the expense reclassification deferred-and-recorded; sold quantities and
the backflush election stay in Phase 4.

Previous milestone: **Procurement module exit: PASS**
(`phase-2-procurement-complete`, commit b865e7d, suite 2362/0).
**Accounting module exit: PASS** (`accounting-module-complete`, commit
1b6e943, suite 2380/0). Both tags pushed; `main` untouched at fd08e4c.
ERRORS 0 · FAILURES 0 · BLOCKERS 0.
CURRENT_TASK: Task 2.17 executed to plan; commit pending only the
affected-domain run (`apps/procurement` + both inventory import test
files) recorded in STEP 19 below. The active /goal directs the remainder
at **Accounting and Procurement completion**: next is Task 2.18 — the
Phase 2 exit gate (all invariants verified, subledger equals the payable
account, GRNI reconciles, fresh database from zero, the definitive
complete suite, tag `phase-2-procurement-complete`, never merged into
main) and both module-exit gates on fresh databases, then the final
report with exact commits, tags and test totals.
LAST_GREEN_COMMIT: bb009e7 (feature, definitive suite 2339/0 on its tree);
the docs checkpoint follows it
LAST_PUSHED_COMMIT: same
WORKING_TREE: clean
RUNNING_TESTS: none
CURRENT_BRANCH: phase/2-procurement (tracking origin)

ACTIVE_WORKTREES:
- `khan-mandi-rms` — phase/2-procurement. Single lane, clean tree.

ACTIVE_DATABASES (none to be dropped):
- `khan_mandi_dev` — development, seeded and visible
- `test_khan_mandi_dev` — test runs
- `khan_mandi_p1_exit` — Phase 1 exit verification, seeded
- `khan_mandi_p2_b4` — Batch 4 verification, migrated from zero and
  seeded; created by Step 12, never to be dropped
- `khan_mandi_p2_b5` — Step 13 verification, migrated from zero and seeded;
  created by Step 13, never to be dropped
- `khan_mandi_p2_b6` — Step 14 verification, migrated from zero and seeded;
  created by Step 14, never to be dropped
- `khan_mandi_p2_b7` — Step 15 verification, migrated from zero through all
  24 procurement migrations and seeded twice with identical counts; created
  by Step 15, never to be dropped
- `khan_mandi_p2_b8` — Task 2.14 verification, migrated from zero through all
  26 procurement migrations and seeded twice with identical counts; created
  by Task 2.14, never to be dropped (later migrated through 0028)
- `khan_mandi_p2_b9` — Task 2.15 verification, migrated from zero through all
  30 procurement migrations and seeded twice with identical counts; created
  by Task 2.15, never to be dropped
- `khan_mandi_p2_b10` — Task 2.16 verification, migrated from zero through
  all 31 procurement migrations and seeded twice with identical counts
  (3 suppliers, 3 orders, 6 receipts, 4 invoices, 2 matches, 3 returns,
  1 note, 1 payment); `verify_procurement` clean, all twelve report routes
  and CSVs 200; created by Task 2.16, never to be dropped
- `khan_mandi_p2_gate` — Phase 2 exit gate, migrated from zero and seeded;
  both verifiers clean, 25 inspection routes rendered; created by Task 2.18
- `khan_mandi_p2_gate2` — Phase 2 exit gate **after** the two 2.18 reviews,
  migrated from zero, seeded twice (3 suppliers, 4 import batches, the demo
  note applied), both verifiers 0 discrepancies, 25 routes 200; the database
  that certifies the tagged tree. Never to be dropped
- `khan_mandi_p2_b11` — Task 2.17 verification, migrated from zero
  (inventory 0020 + procurement 0032 included), seeded twice with identical
  counts (3 suppliers, 3 import batches — two inventory, one procurement,
  all APPLIED), `verify_procurement` clean, the upload screen offering all
  six kinds, HTMX fragments verified; created by Task 2.17, never dropped
- `khan_mandi_t17a_check`, `khan_mandi_t16_check`, `_t15_`, `_t14_`, `_t13_`,
  `khan_mandi_ledger_check`, `khan_mandi_inv_check`, `khan_mandi_freshcheck`

RUNNING_TESTS: none
FAILED_TESTS: none
FIX_BRANCHES: none
ERRORS_REMAINING: 0

NEXT_EXACT_ACTION: the Accounting completion queue, ACCT-1 → ACCT-6,
sequentially, one coherent commit each; then the Accounting module exit gate
on a fresh PostgreSQL database and one definitive complete project suite,
then the Accounting completion tag. Determinations already drafted (outside
the repository, while the Procurement suite held the test database):

- **ACCT-5 is a specification correction, not new API scope.** Task 0.7 §3
  ("API — commands, not CRUD") is the section that *enumerates* deliverable
  endpoints — four journal-entry routes and three period routes, seven in
  total, and `api.py` ships exactly those plus the GET/DELETE §4's first row
  authorises. §4 is a *policy* table whose bold line is a prohibition ("No
  generic writable CRUD for posted accounting records anywhere"); its
  "Accounts, cost centres | Command endpoints" row constrains the form any
  future exposure must take rather than ordering one built, and §7's own
  Notes column against that row records what it verified: "Accounting admin
  is read-only for everyone, superusers included". So: correct §4's row and
  §7's mark to state what exists and what does not, fix `admin.py:177` whose
  `AccountAdmin` docstring sends the reader to endpoints that do not exist,
  and record the position in traceability. Separately and genuinely in
  scope: `amend_account_role_mapping` has no view, URL, route or test while
  its two siblings are wired at `urls.py:19–28`.
- **ACCT-1 maps requirements to tests, not one row per test.** Seventeen
  `ACC-` rows over the twelve kernel invariants, citing the real classes in
  `test_posting.py`, `test_chart_of_accounts.py`, `test_hardening.py` and
  `test_commit_boundary.py` (100 tests, zero rows today). Plus a validator
  that fails when an approved task has **no traceability section at all** —
  the check that would have caught this, since the existing one resolves
  row→test forward only and therefore reports clean.

SUPERSEDED — Task 2.18 (the Phase 2 exit gate, breakdown §2.18) is DONE:
All fifty procurement invariants verified against their cited tests;
supplier subledger equals the payable account and GRNI reconciles (both
already proven by `verify_procurement_accounting` — re-run on the fresh
gate database); no duplicate posting, no cross-tenant access, no scope
leak; demo visible on every route; a fresh database migrated from zero;
the definitive complete project suite exits 0 on the final committed
tree; traceability citing real tests. Exit: tag
`phase-2-procurement-complete`, pushed, **never merged into main**. Then
the Accounting-side module-exit check and the /goal's final report (exact
commits, tags, test totals, Demo/reconciliation status, ERRORS 0,
FAILURES 0, BLOCKERS 0).

NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git branch --show-current                     # expect phase/2-procurement
.venv/Scripts/pytest.exe -q                   # the definitive gate run
```

DEMO_STATE: `khan_mandi_dev` seeded and visible; sign in as `moamel`,
organization DEMO-KHAN-MANDI. Task 2.16 adds the twelve report routes under
`/procurement/reports/…` (all in the seed command's inspection list, all
route-swept 200 with CSVs; a storekeeper answers 403 on every one). The
demo's own numbers reconcile on the screens: aging shows the grocery
supplier 112,000 open (>90 days) with a 10,000 advance and the chicken
supplier a −28,000 net position from the standing credit; return status
shows the chicken claim fully settled (40,514.706 = 40,514.706); the GL
tie-out answers مطابق three times for both organizations. Task 2.14 adds `DEMO-SCN-CHICKEN`
(SCN-2026-000001, **POSTED**): the supplier credits the 28,000 the chicken
was bought for against its 40,514.706 book value — the claim in `8-01-04-001`
closes to **0** and a visible **12,514.706 loss** lands in `7-09-04-001`,
the ADR-022 §2 gap on paper. Unallocated on purpose (the chicken delivery has
no posted invoice), so PRC-051's standing-credit state is on the screen.
Journals **38 → 39**; movements stay 48 — a note moves no stock. The Step 15
demo's chicken `expected_credit_value` was corrected 280,000 → 28,000 for
fresh seeds (the 14,000 was per ten-kilo carton, not per kilo); existing
databases keep the historical metadata, which posts nothing.

Step 15's returns, unchanged beneath it:

- `DEMO-SRET-CHICKEN` — SRET-2026-000001, **POSTED**. Twenty kilograms of the
  warm-chicken delivery (`DEMO-GRN-REJECT`) that passed inspection and
  spoiled on the shelf — beside the thirty that were rejected at the gate, so
  the two mechanisms sit on one document trail. Book value out **40,514.706**
  at the standing average against an expected credit of 280,000 at the
  receipt price — the ADR-022 §2 gap, visible.
- `DEMO-SRET-DRAFT` — five kilograms of rice against `DEMO-GRN-MATCHED`, left
  for the reader to post, and proof that a live match and a live invoice
  posting do not make a delivery unreturnable.
- `DEMO-SRET-REVERSED` — SRET-2026-000002, two kilograms of meat, posted by
  the storekeeper and reversed by the manager (the storekeeper cannot).

Counts: stock movements **45 → 48** (two `RETURN_OUT` plus the meat
reversal), journals **35 → 38**, postings still **3**, locations unchanged.
Balances: `8-01-03-001` still 3,000; `8-01-04-001` (supplier return clearing)
**40,514.706** — exactly the standing posted return, the reversed one nets to
zero; `7-09-04-001` (purchase return variance) **0**, and
`verify_supplier_returns` asserts it stays 0 until Task 2.14.

Routes verified rendering for an authorised user (Django test client with
`force_login`, so no credential is read or typed): /procurement/returns/
(16,696 bytes), the create form (15,705), the posted detail (18,292), the
draft detail (19,531) and the reversed detail (17,900). HTMX verified: the
list answers a fragment only, 2,510 bytes against 16,696. Commands are
POST-only — post and reverse answer 405 to a GET (asserted in tests). The
navigation entry "مرتجعات الموردين" is live and points at the procurement
route — the entry inventory explicitly gave up in Phase 1.

A second and a third seed run change nothing (returns stay at 3, movements at
48, journals at 38).

RECONCILIATION_STATE: clean on `khan_mandi_dev` (both organizations) and on
the fresh `khan_mandi_p2_b7` across all four verifiers — `verify_procurement`
(now also including `verify_supplier_returns`), `verify_organization`,
`verify_inventory_against_gl` and `verify_locations`. The two databases
reproduce each other exactly: 48 movements, 38 journals, 3 postings,
40,514.706 in the return clearing on both.

`verify_grni_clearing` is invariant 47, and two things about it are worth
writing down. **Cleared is not matched**: a draft or ready match consumes
availability and clears nothing, so only allocations under a *live posting*
count. And the check is scoped to **procurement's own** GRNI movement, because
Task 1.4's uninvoiced stock receipts credit the same account and are not this
module's to explain — the unscoped version reported a discrepancy the size of
the inventory demo, which is how the scoping was found.

`verify_parked_variance` proves every fils in `8-01-03-001` traces to a live
posting and to the allocation rows beneath it, and catches a manual journal
against the account.

ACCOUNTING MODULE EXIT: **PASS**, tag `accounting-module-complete` on commit
`1b6e943`. Definitive complete project suite **2380 passed, 0 failed**
(48:21), sole pytest process, on a tree verified byte-identical to its
baseline **and fully committed** — the tag points at the exact certified
commit rather than at a checkpoint above it.

The module is three approved tasks — 0.6 kernel, 0.7 permissions/admin/API,
0.8 completion — plus the mapping surface Task 1.3 added to the same app.
All DONE on code and test evidence. Gate defined in
`docs/specs/accounting-module-exit-gate.md` (seventeen checks) and executed
on `khan_mandi_acct_gate`, created empty and migrated from zero: 13
permissions all scoped, 77 accounts, 6 cost centres, 12 periods and no
thirteenth, 17 system roles, `SourceEvent` closed to exactly POSTED and
REVERSED, the approved RTL screens 200, both admin add pages 403.

**The six completion obligations, and what each actually was.** An
independent seven-agent audit — whose critic re-ran the accounting suite
itself and corrected the audit's own first draft — found six items, none of
them new features:

- **ACCT-1** (`c08118d`). Task 0.6 had *no traceability section and no
  rows*, while a hundred of its tests passed. The checker resolved row→test
  forward only, so a task with no rows passed trivially and the file
  reported clean. Seventeen `ACC-` rows added (requirement-level, not one
  per test) plus a coverage validator that reads the approved tasks from
  CLAUDE.md and the breakdowns; verified both ways — 38 tasks parsed, none
  missing, and exactly `['0.6']` missing when the new section is removed.
  PRM-001 ("twelve" against a test asserting thirteen), EXIT-007 (cited a
  dead selector) and MON-011 (Partial for a tested account) corrected.
- **ACCT-2** (`d40c670`). The two shipped mapping-mutation screens had no
  test anywhere. Eleven added. An earlier draft skipped the used-mapping
  case conditionally; removed, because a skip that hides a gap is worse than
  a reference to where the rule is genuinely held.
- **ACCT-3** (`ea8c983`). ADR-013/014/015 each still said "not yet built"
  for work built in Phase 0, and their "Open" questions had been settled in
  code for many tasks. The decisions index omitted four accepted ADRs and
  called two "proposed". ADR-023 §5 asserted both that an invoice with no
  receipt posts *and* that it refuses; the shipped system refuses, and the
  superseded half survived in three files.
- **ACCT-4** (`6c1f1ac`). Not tidying: `verify_procurement_accounting`
  aggregated the payable balance with **no entry-status filter**, so it
  counted DRAFT entries, which the API can create. It now calls the
  accounting module's own `account_balance` (posted and reversed only),
  which fixes the gap and gives the selector a real production caller.
  `entry_total`, `entries_for_period` and `chart_of_accounts` removed — zero
  callers — and invariant 8's citation corrected, having named `entry_total`
  as its enforcement site.
- **ACCT-5** (`b81e910`). Two questions under one label. The spec: Task 0.7
  §3 enumerates seven endpoints and never listed a chart route; §4 is a
  policy table whose emphasis is a prohibition. So §4's row, §7's "Built"
  mark and the `AccountAdmin` docstring were corrected — **no API scope
  invented**. The real gap: `amend_account_role_mapping` had been a public
  command since Task 1.3 with no view, URL, route or test while ADR-019
  lists amending beside close and archive, both of which had screens. Now
  wired, scoped, and covered by six tests.
- **ACCT-6** (`1b6e943`). The gate itself, demanded three times and defined
  nowhere.

STEP 20 (Task 2.18, Phase 2 exit gate): **COMPLETE — PROCUREMENT EXIT PASS.**
Certifying definitive suite on the final tree: **2362 passed, 0 failed**
(52:19), run on an uncontended machine, and the tree was verified
**byte-identical** to the baseline the run started against (HEAD 812ffcc,
same `git status --porcelain`, same `git diff` hash 9b0926f8) before
anything was committed. Feature commit `b865e7d`, tag
`phase-2-procurement-complete`, pushed; `main` untouched.

**Two runs before it were void, both from the same self-inflicted cause and
worth recording so it does not recur.** A definitive suite and a second
pytest cannot share one PostgreSQL test database: the loser errors at the
session fixture, every test "ERRORs", and the output looks catastrophic
while proving nothing (2362 errors in one case, 40 in another — including
pure-function phone tests that touch none of the changed code, which is the
tell). The first collision was a second suite I launched myself; the second
was the Accounting audit's critic agent independently re-running
`apps/accounting/tests/` to verify the ledger's claims — correct of it to
do, but it had to be serialised against the gate. **Rule: one pytest owner
at a time, and no workflow or subagent that may run tests while a gate suite
is in flight.**

A first definitive
suite passed **2351/0** (5:35:23) on the tree as it stood, and the fresh
`khan_mandi_p2_gate` database migrated from zero, seeded, reconciled clean
on both `verify_procurement` and `verify_inventory_against_gl`, and rendered
all 25 inspection routes. Two human-directed reviews then **changed
executable code**, so that suite is retained as diagnostic evidence only and
does not certify the final tree — one new definitive suite runs on the tree
below before any tag.

**Review 1 — the supplier statement's same-day order.** Task 2.0 §12 defines
the statement as "every document and allocation for one supplier, running
balance" and states *no* ordering rule, so the first cut's document-kind
precedence (charges before settlements) had no approved requirement behind
it. Replaced with the ledger's own chronology: business date → journal
`posted_at` → journal `entry_number` → kind → document number, kind
surviving only as a tie-break so the key keeps a total order. The old rule
was not merely arbitrary but wrong: a payment made Monday and an invoice
posted Tuesday-for-Monday printed with the invoice first, telling a reader
the payment settled a debt that did not yet exist. Six tests in
`TestStatementOrdering` — invoice-then-payment, payment-then-invoice, a
payment between two invoices, a credit note between two events, primary-key
drift (two drafts created in one order and posted in the other), and the
closing balance equalling `supplier_outstanding` under any permutation —
each naming the answer kind-precedence would have given.

**Review 2 — the demo's import evidence.** The first cut seeded one APPLIED
batch whose rows restated the suppliers exactly, so it changed nothing. Task
1.7's own `_applied_import`/`_failed_import` pair is the approved standard
and it is stricter: procurement now seeds an APPLIED batch that makes one
visible, harmless change (a note on one demo supplier through
`update_supplier` — 3 valid rows, 1 applied, the framework's documented
"unchanged is not a failure" on screen) and a FAILED_VALIDATION batch
holding one good row beside a nameless supplier and unparseable terms.
Three tests prove the mutation, that the failed batch applies zero rows and
is refused (`import_not_validated` — the status gate is reached before the
row-count gate), and that a second and third seed add no batch and change
nothing.

**A real defect the review surfaced.** The import writers compared raw CSV
text against **service-normalised** stored values: `create_supplier`
canonicalises the phone (`07701234567` → `+9647701234567`) and strips every
text field. A row restating a supplier exactly as stored therefore reported
`updated` — inflating `applied_row_count` on every re-import and breaking the
framework's "a row asking for what the record already holds counts as
unchanged" contract — and an unusable phone passed validation only to raise
inside `apply_batch`, halfway through an atomic write. Fixed in the
**validator** rather than the writer: cleaned rows now hold exactly what the
service will store, so the preview shows the landing value and a bad phone is
a row error with a row number (`try_normalize_iraqi_mobile`). The same
`strip()` discipline was applied to the catalogue and draft validators. Two
regression tests hold it. It surfaced only because the demo test asserted
1 changed and 2 unchanged; without that assertion every row simply said
"updated" and nothing looked broken.

Focused runs after the changes: report suite **25/25**, import suite
**17/17**, all 13 pre-commit hooks green, `makemigrations --check` clean.
Fresh `khan_mandi_p2_gate2` migrated from zero and seeded twice: 3 suppliers
(PRC-066 intact), 4 import batches (2 inventory, 2 procurement — one applied
showing "3 صالح / 0 مرفوض, طُبِّق 1", one "فشل التدقيق 1 صالح / 2 مرفوض"),
the demo note applied, both verifiers reporting 0 discrepancies, all 25
routes 200.

**Recorded at the 2.18 gate, not changed.** Invariant 50 asks every report to
name its cutoff semantics. The twelve procurement reports inherit the Phase 1
chrome, which prints `mode.label` ("حسب تاريخ الترحيل") even where
`supports_modes` is false — the same thing inventory's expiry, reorder and
waste reports have always done, because they too read current state. The
procurement reports offer no historical mode: they read live documents, and
their Arabic hints say so ("يُحتسب من المستندات المرحّلة ولا يُخزَّن").
Changing the shared label is a Phase 1 chrome decision affecting nine
existing inventory reports as well, so it is recorded here rather than
altered inside a procurement gate.

**Accounting audit completed** (7-agent workflow, independently re-verified by
a critic that re-ran the suite itself: 290 passed, exit 0). The Accounting
module is exactly three approved task IDs — 0.6 kernel, 0.7
permissions/admin/API, 0.8 completion — and all three are DONE on code and
test evidence. Five items are DEFERRED with a document and a stated reason
(PRC-044, ADR-022 §5, ADR-023 §5, four SourceEvent values, the Phase 5
screens). Everything the charter scopes to Phase 5 — trial balance, general
ledger, P&L, balance sheet, cashboxes, bank accounts and the 13 inert
navigation sections — is future-phase work, not open Accounting work; no
Phase 5 task ID exists in the repository. Six OPEN items remain, none of them
new features — they are completion obligations on already-approved tasks, and
they are queued as ACCT-1..ACCT-6:
1. **ACCT-1** Task 0.6 has *no traceability section at all*; 100 kernel tests
   are cited by zero rows, and `tests/test_traceability.py` resolves
   row→test forward only so it reports clean and structurally cannot see it.
   Also: PRM-001 says "Twelve" permissions where thirteen exist and its own
   cited test asserts thirteen; EXIT-007 cites a dead selector; MON-011 is
   still Partial though the account is seeded and tested.
2. **ACCT-2** `accounting:mapping_close` and `accounting:mapping_archive` are
   shipped, linked mutation surfaces with zero tests anywhere.
3. **ACCT-3** ADR-013/014/015 all still say "not yet built" for built work;
   `decisions/README.md` and the root `README.md` are stale in both
   directions; ADR-023's superseded prose survives in three places against a
   shipped system that refuses to post.
4. **ACCT-4** The *entire* `apps/accounting/selectors.py` is production-dead
   (imported only by tests), and `accounting-kernel-invariants.md:18` cites
   the dead `entry_total` as invariant 8's enforcement site — while inventory
   and procurement reconciliations hand-roll seven JournalLine aggregations
   the module already provides.
5. **ACCT-5** Task 0.7 §4 promises account/cost-centre command endpoints and
   §7 marks them "Built"; `api.py` has none, and `admin.py` tells the reader
   to use them. `amend_account_role_mapping` has no surface at all.
6. **ACCT-6** The "Accounting-side module-exit check" the runbook demands
   three times is defined nowhere.

STEP 19 (Task 2.17, imports and hardening): **COMPLETE** (commit follows the
affected-domain run: `apps/procurement` plus both inventory import test
files). The preflight plan in the previous checkpoint was executed exactly:
`ImportKind` gains `SUPPLIER` / `SUPPLIER_ITEM` / `PURCHASE_REQUEST_DRAFT`
(the last branch-scoped; inventory migration 0020 regenerates both kind
constraints), and `apps/procurement/imports.py` registers validators,
writers, `EXTERNAL_KEYS` compound identities and `KIND_PERMISSIONS` entries
from `AppConfig.ready` — the two registry hooks added to the framework fall
back to unchanged Task 1.7 behaviour, and inventory never imports
procurement. Writers call only the real services; the draft kind groups
rows sharing (warehouse, required date, purpose) into one reviewable draft
with no number and no submission; §16.8 is held by vocabulary and asserted
(`test_no_import_kind_names_a_posted_document`). New organization-scoped
`import_supplier` / `import_supplier_item` (migration 0032) granted to
manager and purchasing; the draft kind rides `create_purchase_request` —
recorded assumption: importing a draft requires exactly the authority to
create one. Demo: one applied supplier batch restating the three demo
suppliers verbatim (all actions `unchanged`, PRC-066 intact), idempotent by
content hash (dev re-seed: still one batch); inspection list gains
`inventory:import_list`. 10 procurement import tests green standalone; the
framework's 58 pass with the six-kind positive twin replacing the
three-kind boundary. Dev migrated; fresh `khan_mandi_p2_b11` verified as
the header records. No mandated full-project suite at this boundary — the
2.18 exit gate runs it.

STEP 18 (Task 2.16, reports and reconciliation): **COMPLETE**. Definitive
complete project suite on the final tree: **2339 passed, 0 failed** (47:35);
the code was untouched between that run and the commits — only this runbook.
Twelve GET-only reports on the Phase 1 machinery **inherited, not imitated**:
`ProcurementReportView` subclasses `InventoryReportView`, so the shared
data-driven template, the CSV-equals-screen export, formula neutralisation,
provenance headers, pagination and the HTMX fragment fallback are the code
Phase 1 certified — what changed is the entry permission (new
organization-scoped `view_procurement_report`, migration 0031, granted to
manager/accounting-manager/accountant/purchasing), cost redaction through
`view_supplier_cost` (omitted, never blanked), and a `supplier_id` filter on
a `ReportFilters` subclass. Every figure is the verifiers' own derivation —
`outstanding_amount`, `unallocated_credit`, `advance_remainder`,
`settled_book_value_for`, the GRNI clearing arithmetic — never a second
formula. `verify_procurement_accounting` (PRC-058) composes equalities 1–3
under `verify_procurement` — open balances vs the payable account
(whole-account, procurement-exclusive), GRNI via the delegated
`verify_grni_clearing` (scoped, shared with Task 1.4), source identity
across all six source types — with equality 4 re-checked by the
per-document verifiers alongside. Navigation promotes "أرصدة الموردين" to
the aging report: the balance is derived, so the report *is* the balances
screen. The 19-test suite covers the permission sweep (twelve routes × three
actors), both scope arms (a branch post reaches the organization exactly as
`visible_supplier_invoices` certifies; a hand-granted permission names no
post and reaches nothing), row- and CSV-level redaction, per-report
correctness on service-built scenarios, the matching-lifecycle walk (one
3,000 variance moving through invoice-without-receipt → matching-exceptions
→ price-variance while the GRNI exception clears), the partial credit-note
settlement walk (قائم → جزئي), the clean and planted GL tie-outs, hostile-name
neutralisation and the HTMX fragment.

**The full suite caught what standalone runs could not.** The statement's
same-day tie-break ordered rows from three different tables by raw pk — an
accident of sequence allocation that standalone runs reproduced and the
2,339-test run's drifted sequences exposed (first run 2338/1). Replaced with
charges-before-settlements then document number: deterministic, and the
honest answer given that documents carry a business date, not a time. The
order-independence was then proved cheaply (payments tests then report tests
in one session) before the suite re-ran green.

**Demo tooling hardened in passing.** A crashed seed run (procurement demo
before the inventory scenario, so no lots existed) left an empty draft
receipt that every later run reused and failed to post — permanent
non-idempotence. `seed_demo_receipts` now *finishes* a half-built draft
(reuses the row, adds the lines the crash never reached) instead of
reusing it empty; found on the b10 fresh database, fixed, and both seed
passes then produced identical counts. The matching-exceptions read also
excludes matches behind a live posting — once posted, a variance is an
explanation in the price-variance report, not a pending decision.

STEP 17 (Task 2.15, supplier payments): **COMPLETE**. Definitive complete
project suite on the final tree: **2320 passed, 0 failed** (48:16); the code
was untouched between that run and the commits — only this runbook. Three
stale boundary assertions surfaced by the first run and fixed before it:
the chart counts 74 → 77, and Step 12's "payment roles still unseeded"
marker replaced by its positive twin, exactly the discipline that marker
existed to enforce. §11's journal verbatim:
`Dr SUPPLIER_PAYABLE allocated / Dr SUPPLIER_ADVANCE remainder / Cr
cash-or-bank full amount`, the source resolved by `method` through the two
new payment roles (PRC-056) and the remainder an asset in the new
`1-04-01-001` (chart 74 → 77, phase-0 exit count updated), never a negative
payable (PRC-055). Accounting migration 0014 seeds the three roles;
procurement migrations 0029/0030 add the models and their whole-row guards.
The allocation bound is `outstanding_amount` — ONE expression netting posted
credit notes and posted payments, which also removed a double-subtraction
the credit-note bounds had picked up. `verify_supplier_payments` proves each
payment's journal against its allocations plus the organization-wide advance
balance; `verify_supplier_payables` nets posted payments' allocated share.
Four organization-scoped permissions with the invoice's maker-checker split
(manager records and gets 403 on post; accounting manager releases the
money), Arabic RTL screens with the oldest-due-first visible ordering
(PRC-057), API commands, read-only admin, navigation promotion of
"دفعات الموردين" (allocations live on the payment detail, so "تخصيص
الدفعات" needs no separate route), `DEMO-SPAY-GOODS` (SPAY-2026-000001:
60,000 by bank, 50,000 allocated against the 87,000 rice bill, a real
10,000 advance standing; journals 39 → 40; idempotent), 17 payment tests
and 3 real-COMMIT races. **Deferred, recorded:** consuming a standing
advance or standing credit against a later invoice has no approved journal
shape and awaits its own task.

STEP 16 (Task 2.14, supplier credit notes): **COMPLETE**. Definitive complete
project suite on the final tree: **2300 passed, 0 failed** (46:44); the code
was untouched between that run and the commits — only this runbook. 39
credit-note tests, 3 real-COMMIT races, 231 across the affected return,
invoice and variance suites. The recognising
entry ADR-022 §2 (as amended) deferred — **partial, per line, across notes**,
per the human's design correction issued before the commit: the first cut's
one-standing-note-per-return rule was removed, and a note settles its return
through explicit `SupplierCreditReturnAllocation` rows against the return's
lines. A note may cover several lines; a line may be settled by several
notes; the bound is the line's returned quantity and posted book value; a
partial slice settles the quantized proportional share of the line's
*remaining* claim and the final slice takes the exact remainder, so active
settlements plus the open claim equal the line's book value to the fils and
no rounding residual can strand in the clearing account. The journal: `Dr
SUPPLIER_PAYABLE amount / Cr SUPPLIER_RETURN_CLEARING settled book value /
Cr-or-Dr PURCHASE_RETURN_VARIANCE difference`, the variance line absent when
they agree. Migration 0027 backfills every note posted under the old rule
with the allocations its journal implies.
`SupplierCreditAllocation` nets the note against posted invoices;
`outstanding_amount` and `supplier_outstanding` subtract posted notes; the
unallocated remainder is PRC-051's standing credit — an allocation state
against the payable, never a separate account (and never `SUPPLIER_ADVANCE`,
which is cash paid before an invoice — a different economic event), or
invariant 46 dies.
`PURCHASE_RETURN_VARIANCE` is mapped now, the act Task 2.13 refused because
nothing posted to it. **Scope recorded rather than implied:** a Release 1
note must cite a posted return; invoice-only and reference-free notes have no
approved contra account anywhere and are refused by the model's shape — the
invoice-before-receipt precedent, applied again.

**The Task 2.13 lesson generalized before it bit.** The invoice reversal
guard walked only line relations, and a credit allocation cites the invoice
at the **header** — `test_a_posted_notes_allocation_blocks_the_invoice_reversal`
failed against the unmodified guard, proving a posted note's netting could be
reversed out from under it. The guard now walks the header too, which
required `PurchaseMatch` to declare `live_dependency = Q(status__in=("DRAFT",
"READY"))` — a cancelled match is history, not a dependent — so the
documented reverse-and-rematch correction kept working (all 264 affected
invoice/matching/return tests pass).

32 credit-note tests, 3 real-COMMIT races (two posts of one note; two notes
racing one return; two notes' allocations racing one invoice remainder),
maker-checker exercised through the routes (manager records and gets 403 on
post; accounting manager posts). Fresh `khan_mandi_p2_b8` from zero through
all 26 procurement migrations, seeded twice, identical counts, 30 routes
rendered, all verifiers clean — including the new
`verify_supplier_credit_notes` (per-note journal equalities plus clearing ==
unsettled standing returns and variance == Σ agreed-versus-book).
`verify_supplier_returns`' "variance is empty" boundary check moved into it.
Gates: ruff, format, mypy (235 files), check, makemigrations --check all
pass.

STEP 15 (Task 2.13, supplier returns): **COMPLETE**. Definitive complete
project suite on the final tree: **2258 passed, 0 failed** (52:07); the code
was not touched between that run and the commits below — only this runbook
and the docs. Four commits: `fce9a4a`
(inventory: `RETURN_OUT`), `a0a0fc2` (accounting: the two return roles and
chart accounts, 70 → 74), `e947941` (procurement: the document, the posting,
the reversal, the verifier, 30 tests), and `3bb8194` — selectors, API
commands, Arabic RTL screens, the navigation
promotion, read-only admin, three demo returns, 15 more surface tests and 4
real-COMMIT races. Fresh database `khan_mandi_p2_b7` migrated from zero
through all 24 procurement migrations, seeded twice with identical counts,
every return route rendered, all four verifiers clean on it and on
`khan_mandi_dev` — and the two databases reproduce each other exactly.
Quality gates: ruff, ruff format, mypy (232 files), manage.py check,
makemigrations --check, pre-commit 13 hooks — all pass.

**The design decision that shaped the step** was put to the human twice. The
authoritative answers to B1/B2 (debit GRNI/payable per allocation, reverse
PPV) and B3/B5 (clearing account only, no variance at the return) prescribed
two mutually exclusive postings for the same event; the human chose **B3/B5**:
the physical return posts `Dr SUPPLIER_RETURN_CLEARING (8-01-04-001) /
Cr INVENTORY_CONTROL` for the book value at the standing average, and nothing
else. No payable, no GRNI, no variance — at the gate nobody knows what the
supplier will credit. The clearing balance **is** the claim outstanding;
Task 2.14's credit note clears it and recognises the difference in
`PURCHASE_RETURN_VARIANCE` (7-09-04-001, class OTHER, seeded and deliberately
**unmapped** — a test asserts the unmapped state). ADR-022 is Accepted in
full with the §2 amendment; Task 2.0 §10/§13/§15 amended; invariants rows
38–40e; traceability PRC-047..050 updated.

**A race found a real hole, fixed here.** Task 2.9's receipt-reversal guard
walked only receipt-*line* relations, and a supplier return cites the receipt
at the **header** before its first line exists in a separate transaction — so
`test_a_new_return_racing_the_receipt_reversal` produced a reversed delivery
with a standing draft return against it. The guard now walks the header's
relations too (both services lock the receipt row first, so the race
serializes), and `SupplierReturn.live_dependency` was corrected from
`Q(supplier_return__status__in=…)` — the line model's shape, a FieldError if
ever applied to the header's own queryset — to `Q(status__in=("DRAFT",
"POSTED"))`. A second property worth writing down: a **draft** return
consumes availability the moment its line is added, under a lock on the
receipt line, so the sum of standing returns cannot exceed the accepted
quantity through any interleaving — the interesting quantity race is two
*adds*, not two posts, and the posting-time re-check stays as depth.

Two smaller things: `tests/test_phase_0_exit.py`'s chart count moved 70 → 74
(stale assertion, found by the exit test itself), and the demo needed
`SUPPLIER_RETURN_CLEARING` added to `PROCUREMENT_ACCOUNT_MAPPINGS` plus a
`seed_chart_of_accounts` re-run on `khan_mandi_dev` (4 accounts created) —
the variance role is deliberately not in the demo mappings either.

BATCH 4 CERTIFICATION (Steps 10–12): **PASS** at 64d94f8. Complete project
suite 2053 passed, 0 failed. Fresh database `khan_mandi_p2_b4` migrated from
zero, roles and permissions seeded, both demo seeds run twice with identical
counts, every procurement route rendered, all four verifiers clean. Quality
gates: ruff, ruff format, mypy (224 files), manage.py check, makemigrations
--check, pre-commit 13 hooks — all pass.

STEP 13 (Task 2.11, three-way matching): **COMPLETE** at 0c2ee51.

STEP 14 (Task 2.12, price and quantity variance accounting): **COMPLETE** at
29b0ea0. Complete project suite 2203 passed, 0 failed; procurement 580, of which 54 are new plus four real-COMMIT races. Fresh database
`khan_mandi_p2_b6` migrated from zero through all 21 procurement migrations,
seeded, both demo seeds run three times with identical counts, every matching
and invoice route rendered, all four verifiers clean on it and on
`khan_mandi_dev` — and the two databases now reproduce each other exactly.
Quality gates: ruff, ruff format, mypy (229 files), manage.py check,
makemigrations --check, pre-commit 13 hooks — all pass.

**Four blocking conflicts were put to the human before any code was written**,
and their answers shaped the design:

1. *The variance account.* Task 2.0 §15's `5-02-01-001` is cost of sales, which
   sets `requires_cost_center` — and a supplier invoice has no cost centre to
   give. ADR-022 separately rejects booking a purchasing outcome as food cost.
   Resolved: a **clearing** account, `8-01-03-001`, parked rather than
   classified, with the period-end split recorded as a required future step.
2. *PRC-044.* Deferred and formally **not elected**: its permission, source
   identity, allocation policy and reversal rules are undefined, and a partial
   valuation rule is worse than none.
3. *Reversibility.* The invoice's own dependency guard counted the match
   allocations that are the precondition for posting, so every matched invoice
   would have been permanently irreversible. Resolved with posting
   **generations**: reversal marks the generation REVERSED, then cancels the
   match, then checks dependents — and a reversed generation governs nothing.
4. *Cancellation.* Nothing stopped a READY match being withdrawn after its
   invoice posted, which would release a delivery the ledger had already
   cleared with both sides of every Task 2.11 equality moving together, so no
   verifier would have noticed. Refused now in the service **and** by a
   database trigger.

Two further defects were found and fixed in passing: `verify_supplier_invoice`
compared total debits and total credits against the line total, which fires on
a correct *cheaper* invoice and is blind on a dearer one — now account-aware;
and `posted_amount` held only the direct-charge total, which would have
understated every matched invoice in `verify_supplier_payables` by the goods
portion.
Complete project suite 2143 passed, 0 failed; procurement 521, of which 84 are
new (73 matching, 6 matching races, 5 on the direct-account preflight). The
0c2ee51 commit message says "526 of them procurement, 79 new" — both figures
were estimated before the final collection and are wrong; this line is the
record, and the commit was not amended because it had already been pushed. Fresh database `khan_mandi_p2_b5` migrated
from zero through all 16 procurement migrations, seeded, both demo seeds run
twice with identical counts, every matching route rendered, all four verifiers
clean on it and on `khan_mandi_dev`. Quality gates: ruff, ruff format, mypy
(228 files), manage.py check, makemigrations --check, pre-commit 13 hooks —
all pass.

Three defects were found and fixed rather than worked around:

1. **A production hole in Step 12.** `invoices.py::_validate_direct_account`
   accepted any postable account, including Inventory control, GRNI and
   Supplier payable — so a direct charge line could credit the payable twice
   or clear GRNI without any match. Now refused twice over: by account class
   (`account_class_not_billable`) and by role ownership
   (`account_is_role_owned`), because a role-owned account belongs to the
   posting rule that owns it, not to whoever picks it from a dropdown.
2. **A constraint of my own that broke the correction path.** The first
   version of `procurement_match_unready_carries_no_readiness` refused to
   *cancel* a READY match, since cancelling leaves `ready_by`/`ready_at` in
   place. Corrected to `procurement_match_draft_carries_no_readiness`
   (migration 0016): only a draft may carry no readiness, and a cancelled
   match keeps the evidence of when it was agreed.
3. **Money rendered through float on every procurement screen.**
   `{{ value|stringformat:"f" }}` is printf: it converts through float — which
   CLAUDE.md forbids outright — and prints six decimals whatever the column
   holds, so 84,000 IQD rendered as `84000.000000` and a 3-dp quantity as
   `60.000000`. Task 2.11's three templates now use `|money_full` and a new
   `|quantity` filter (`apps/core/templatetags/quantity_tags.py`, six tests).
   **The rest of the module still has it** — `supplier_invoice_detail.html`,
   `goods_receipt_detail.html` and the order and quotation screens — and that
   sweep is deliberately left out of this commit so the diff stays one
   concern. It is filed as its own task.

A fourth thing was learned rather than fixed: Task 2.9's
`_require_no_downstream_dependency` walks a receipt line's relations rather
than a list of imports, so declaring `match_allocations` gave the receipt its
guard with nobody remembering to add one. That worked exactly as designed —
but it counted cancelled matches too, which would have made cancelling, the
documented correction, leave the delivery permanently unreversible. Models now
declare `live_dependency` (a `Q`) to say which of their rows still stand; the
default without one is still "every row", which is the safe answer for a
relation nobody has considered.

ASSUMPTIONS:
- An award requires a reason unconditionally, not only when the winner is
  dearer as PRC-017 states. Stricter than specified, and deliberate: a
  field that is usually empty is a field nobody reads.
- Purchasing issues but never approves a purchase order. The spec does not
  name the split; it follows the same separation the request already uses.

- Task 2.0 §7 gives the receipt three statuses only: DRAFT, POSTED,
  REVERSED. Inspection is line data, and readiness is derived
  (`is_ready_to_post`), so no INSPECTED/READY state was invented.
- Delivery-reference uniqueness is scoped per supplier, not globally.
- An inspected-but-unposted receipt reserves nothing on the order;
  `add_receipt_line` compensates by also subtracting other drafts.

- Task 2.9. A receipt where **every** line was rejected is refused rather
  than posted as a zero-effect physical record. Task 2.0 §7 gives the
  document three statuses and no zero-value posted state, and inventing one
  would mean inventing accounting for it. The rejection stays recorded on
  the draft, which is where the supplier claim lives anyway.
- The posting idempotency key is derived from the receipt's own `public_id`
  rather than accepted from the caller. Posting *this receipt* is the whole
  command and a posted receipt is frozen by a trigger, so a retry cannot
  present the same key with a different payload. The kernel still refuses a
  duplicate on source identity independently.
- A reversal picks stock back out of the bin the receipt filled, but only as
  much as that bin still holds; the kernel's deterministic auto-release
  covers any remainder. Moving goods between shelves is ordinary warehouse
  work and must not make a reversal impossible. The warehouse total falls by
  exactly the accepted quantity either way, which is the invariant.
- `post_goods_receipt` re-reads the order under a lock and refuses a
  cancelled one, even though `create_goods_receipt` already checked. The
  order can be cancelled while a draft sits on somebody's screen, and
  posting is the act that makes the answer permanent.
- Command endpoints are POST-only URL routes rather than Django Ninja
  operations, because that is the convention every procurement command
  already follows and inventory exposes no posting command over its API
  either. There is deliberately no writable generic CRUD over a posted
  receipt.

- Task 2.10. **A supplier invoice carrying a goods line cannot post here.**
  Task 2.0 §9 posts the matched receipt value to GRNI and the difference to
  purchase price variance, and both come from a Task 2.11 match allocation.
  Posting the invoiced amount to GRNI instead would balance and be wrong.
  The whole document waits in APPROVED with `invoice_awaiting_matching`;
  half-posting would create a payable for part of what is owed.
- **Invoice-before-receipt is not supported**, and that is a reading of the
  spec rather than an omission: §15's account-role table contains no
  `UNRECEIVED_INVENTORY_CLEARING`, so the clearing route has no approved
  account behind it.
- Supplier invoice numbers are compared **case-insensitively**, which Task 2.0
  does not state either way. Stricter than specified and deliberate: the
  reference is stored exactly as the supplier wrote it and folded only for
  the uniqueness key, and paying the same invoice twice is the expensive
  direction to be wrong in. Leading zeros and internal spacing are preserved,
  so `INV-001` and `INV-0001` remain different documents.
- A direct account line lets the person entering the invoice **choose the
  account**. PRC-034 forbids a *posting service* naming an account, and none
  does — `SUPPLIER_PAYABLE` still resolves through an effective-dated role.
  Which expense a delivery charge belongs to is a judgement only the person
  holding the document can make; the alternative is a role per expense
  category, invented by us.
- Every expense account in the seeded chart sets `requires_cost_center`, so
  every demo and test expense line names one. That is the chart's policy
  working, not a workaround.
- `AccountRoleDomain` gains `PURCHASING`. The enum's own docstring invited it
  ("Purchases, Sales and Payroll add their own values when their posting
  rules arrive"), and a supplier payable filed under `INVENTORY` would make
  the domain column a label rather than a fact.
- Only `SUPPLIER_PAYABLE` of Task 2.0 §15's five new roles is seeded. A role
  with no posting rule behind it is a mapping an accounting manager can be
  asked to fill in for a workflow that does not exist — the mistake
  `import_opening_draft` records in inventory.
- Approval is **not** maker-checker at the database. Task 2.0 states that rule
  for the purchase request (PRC-010) and not for the invoice, so no
  `approved_by != created_by` constraint was invented. The role map achieves
  the separation instead: `ACCOUNTANT` creates and cannot approve;
  `ACCOUNTING_MANAGER` approves, posts and reverses, and cannot create.

BLOCKERS: none

## Phase 2 starting facts

- Reuse `GOODS_RECEIVED_NOT_INVOICED` (already mapped, account `2-01-02-001`).
- Supplier balances are derived from documents, never a mutable field.
- Goods receipt posts through the existing inventory kernel; procurement adds
  no second posting path.
- Demo: three suppliers (DEMO-MEAT-SUPPLIER, DEMO-CHICKEN-SUPPLIER,
  DEMO-GROCERY-SUPPLIER) against the existing five items.
- `view_supplier` is Django's **builtin** view permission for the `Supplier`
  model, not a custom one. Declaring it in `Meta.permissions` is an
  `auth.E005` clash. The codename is still `procurement.view_supplier`.
- Every lifecycle guard re-reads its document under a row lock rather than
  trusting the instance it was handed. Found three times; now stated once
  in `apps/procurement/lifecycle.py::lock_and_require_status` and covered
  by `TestTheStaleInstanceRule`. Every new lifecycle service uses it.
- Demo and test dates must stay meaningful as the calendar moves. Two
  defects came from validity windows that had silently expired.
- `_require_draft` re-reads status under a row lock rather than trusting the
  instance handed to it. A stale in-memory DRAFT would otherwise let a line
  be added to an approved request, and no constraint could catch it.
- `ProcurementDocumentSequence` is procurement's own gapless counter.
  Numbers are drawn at submission, never at creation.
- `apps/procurement/views.py` subclasses the list/write/action bases from
  `apps.inventory.views` rather than copying them. Extracting them into
  `apps.core` is worth doing when a third module needs them; it is a refactor
  of certified code and does not belong inside a feature task.

## Closed follow-ups

Both Phase 1 hygiene items are done.

- **Traceability** (`e1afe79`). All 60 aspirational rows reconciled; 275
  unanchored citations given their file; `tests/test_traceability.py` fails the
  build if a row cites a test that does not exist.
- **Suite runtime** (`d9ff702`). The three demo-seeding files went from 18:48
  to 2:05 on the same 123 tests, by sharing one seed per module. Two
  transactional classes moved to `test_location_concurrency.py` and
  `test_import_constraints.py`; `refuse_transactional_tests` stops the
  incompatible combination from ever going quietly green. The move broke
  two traceability citations, which the full suite caught and `51024b6`
  fixed forward.

Measured honestly: the three files went 18:48 → 2:05 back to back on the
same machine, but the **full** suite went 38:42 → 34:56, not the ~22
minutes that saving alone implies. The file-level number is a controlled
comparison; the full-suite number is against a baseline taken on a
different day. Where the rest of the difference went is not established,
and no further optimisation was attempted — the known repeated setup was
the item, and it is addressed.

## Step log

| Step | Status | Commit / tag | Notes |
|---|---|---|---|
| 0A Task 1.7A | **CERTIFIED, PUSHED** | d6044e9 + 45136af | 1597 passed |
| 0B Task 1.7B | **CERTIFIED, PUSHED** | e49da77 | 1613 passed |
| 01 Inventory exit | **COMPLETE** | tag `phase-1-inventory-complete` | fresh DB, 29 routes, pre-commit green |
| B1 Traceability | **COMPLETE, PUSHED** | e1afe79 | 0 unresolved citations, was 233 |
| B2 Suite runtime | **COMPLETE, PUSHED** | d9ff702 | 123 tests, 18:48 → 2:05 |
| 02 Procurement spec | **COMPLETE, PUSHED** | d6c2b0f | 67 PRC requirements, 50 invariants, ADR-022/023 |
| 03 Supplier master | **COMPLETE, PUSHED** | be918c0 | 41 tests, 3 demo suppliers, route + htmx verified |
| 04 Supplier catalogue | **COMPLETE, PUSHED** | 637bd16 | 36 tests, 6 rows, gist no-overlap, AST boundary test |
| 05 Purchase requests | **COMPLETE, PUSHED** | 1692d50 | 39 tests, 4 demo requests, maker-checker at the database |
| 06 Supplier quotations | **COMPLETE, PUSHED** | 63c82be | 35 tests, 2 demo offers, derived totals |
| **Batch 2 cert (04–06)** | **PASS** | 63c82be | 301 tests, verifiers clean, no procurement posting |
| 07 Comparison and award | **COMPLETE, PUSHED** | c169941 | 28 tests, ranking inversion visible on the route |
| 08 Purchase orders | **COMPLETE, PUSHED** | c89ac1d | 36 tests, 3 demo orders, chain visible end to end |
| 09 PO change control | **COMPLETE, PUSHED** | ee2365e | 28 tests, versioned history, shared lifecycle helper |
| **Batch 3 cert (07–09)** | **PASS** | ee2365e | 414 tests, verifiers clean, no procurement posting |
| 10 Goods receipt | **COMPLETE, PUSHED** | aa12633 | 50 tests, 4 demo drafts, seam activated, no posting path |
| 11 Receipt posting + GRNI | **COMPLETE, PUSHED** | ca18fc9 | 346 tests, 6 real-COMMIT races, demo 39→44 movements and 25→30 journals |
| 12 Supplier invoices | **COMPLETE, PUSHED** | 64d94f8 | 91 tests, 4 demo invoices, payable derived, matching boundary held |
| **Batch 4 cert (10–12)** | **PASS** | 64d94f8 | 2053 project tests, fresh DB from zero, four verifiers clean |
| 13 Three-way matching | **COMPLETE, PUSHED** | 0c2ee51 | 2143 project tests; see the Step 13 note above |
| 14 Variance accounting | **COMPLETE, PUSHED** | 29b0ea0 + 61552a1 | 2203 project tests, generations, PPV parked |
| 15 Supplier returns | **COMPLETE, PUSHED** | fce9a4a + a0a0fc2 + e947941 + 3bb8194 | fresh DB b7, verifiers clean, ADR-022 accepted in full |
| 17 Supplier payments | **COMPLETE, PUSHED** | 772607b + checkpoint | fresh DB b9, §11 verbatim, advance never a negative payable |
| 18 Reports + reconciliation | **COMPLETE, PUSHED** | feature + checkpoint | fresh DB b10, twelve reports on the Phase 1 base, PRC-058 composed, suite 2339/0 |
| 19 Imports + hardening | **COMPLETE, PUSHED** | feature + checkpoint | fresh DB b11, three kinds registered into the Task 1.7 framework, §16.8 by vocabulary |
| 16 Supplier credit notes | **COMPLETE, PUSHED** | e26a051 + checkpoint | fresh DB b8, ADR-022 fully implemented, invoice guard hole closed |
| 20 Phase 2 exit gate | **COMPLETE, TAGGED** | b865e7d + checkpoint, tag `phase-2-procurement-complete` | suite 2362/0 on a verified-identical tree; fresh DB gate2; two reviews applied; one real import defect closed |
| ACCT-1..6 | in progress | — | Accounting completion queue; the goal needs an independent Accounting exit PASS |
