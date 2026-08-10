# Overnight autonomous delivery — progress

Checkpoint contract for the 20-step pipeline. Updated after every completed
step and at least every 30–45 minutes. Chat memory is not the record; this is.

---

CURRENT_PIPELINE_STEP: Prerequisite 0A — certify Task 1.7A
CURRENT_TASK: Task 1.7A (reports, exports, imports, projection verification)
LAST_GREEN_COMMIT: 35420a6 (Task 1.6a, pushed)
LAST_PUSHED_COMMIT: 35420a6
CURRENT_BRANCH: phase/1-inventory

ACTIVE_WORKTREES:
- `khan-mandi-rms` — phase/1-inventory @ 35420a6, 24 files staged (1.7A candidate). **FROZEN, under certification.**
- `khan-mandi-17b` — phase/1-location @ 4beffae (1.7B spec + this runbook). Lane F/I.

ACTIVE_DATABASES:
- `khan_mandi_dev` — development, seeded with DEMO-INVENTORY-V1
- `test_khan_mandi_dev` — in use by the running certification suite. **Do not touch.**
- `khan_mandi_t17a_check` — 1.7A fresh-database verification (keep)
- `khan_mandi_freshcheck`, `_inv_check`, `_ledger_check`, `_t13_check`, `_t14_check`, `_t15_check`, `_t16_check` — earlier verification databases (keep)
- 1.7B lane database: **not yet created** — needs env vars supplied to the process, see BLOCKERS

RUNNING_TESTS: full suite on the 1.7A candidate, `scratchpad/final_suite2.txt`
FAILED_TESTS: none so far (58% at last check)
FIX_BRANCHES: none open
ERRORS_REMAINING: 0

NEXT_EXACT_ACTION: on suite green — commit and push Task 1.7A from the main worktree
NEXT_EXACT_COMMAND:
```
cd "C:/Users/muama/Desktop/Khan Mandi/System/khan-mandi-rms"
git commit -F <message file>   # feat(inventory): add reports imports and projection verification
git push origin phase/1-inventory
```

DEMO_STATE: `khan_mandi_dev` seeded; second run `0 created, 79 reused`; reorder BELOW/AT/ABOVE, expiry EXPIRED/WITHIN_30/WITHIN_90, one APPLIED and one FAILED_VALIDATION import batch
RECONCILIATION_STATE: `verify_inventory_accounting`, `verify_stock_ledger`, `verify_stock_projection` all clean on DEMO-KHAN-MANDI

BLOCKERS: none hard.
- **The 1.7B worktree cannot commit Python.** Its pre-commit hooks run mypy and
  `makemigrations --check`, both of which need `DJANGO_SECRET_KEY` and the
  database credentials from `.env` — which is gitignored and lives only in the
  main worktree. Reading, copying or linking that file is forbidden, and
  skipping hooks is forbidden, so the lane is docs-only.
  **Resolution:** the lane did its job (spec + model drafting in parallel with
  certification). Once 1.7A is pushed, `phase/1-location` continues in the main
  worktree, which has the environment. The `khan-mandi-17b` worktree is then
  removed. Its `.venv` is a directory junction — interpreter only, no secrets.
- Uncommitted in `khan-mandi-17b`: `apps/inventory/models.py` with the four
  location models, lint-clean and AST-verified. Carry it forward on rebase.

ASSUMPTIONS:
- `POSTED_AS_OF` is the default report mode: it reproduces a previously printed
  figure, which is what a reader who did not choose is usually doing.
- Import atomicity is all-or-nothing; 99-of-100 was considered and rejected.
- Projection repair mode deferred — verification is safer than an inadequately
  controlled repair.
- `import_opening_draft` reserved, granted to no role, following the
  `override_negative_stock` precedent.
- Locations carry quantity only (ADR-018 §2); the valuation key is unchanged.

---

## Step log

| Step | Status | Commit | Notes |
|---|---|---|---|
| 0A Task 1.7A | CERTIFYING | — | 24 files staged; suite running |
| 0B Task 1.7B | SPECIFIED | 4beffae | spec only, no implementation |
