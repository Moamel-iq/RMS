# Task 8.1 — Jadwa Insights, Stage 1 (production MVP)

**Branch:** `insights-master` · **Status:** delivered, pending the validation
results recorded at the end of this document.

The read-only intelligence layer's first vertical slice: the insight kernel,
one detector, and the two screens that make a finding readable and disputable.

---

## 1. What shipped

| | |
|---|---|
| App | `apps/insights` — models, migrations, permissions, detector registry, orchestration, selectors, views, templates, command, tests |
| Detector | `inventory-issue-coverage-gap` only |
| Screens | `/insights/` dashboard · `/insights/<public_id>/` detail |
| Lifecycle | acknowledge · dismiss (reason required) · explicit reopen |
| Execution | `manage.py run_insights --organization CODE [--branch CODE] [--window 28d]` |
| Navigation | «التحليل الذكي» at position 7, gated by the existing access derivation |

### Deliberately deferred

Every other detector; executive KPI dashboards and charts; a threshold-management
UI; PDF/XLSX/digest/report surfaces; aggregate or cache tables; scheduler
infrastructure; the optional AI narrative layer; and any in-app "run now"
button — see §7.

---

## 2. The kernel, and the contradiction it resolves

A finding is two incompatible things: a **living case** somebody acknowledges
and watches, and a **historical record** whose figures must still read the same
next year. One mutable row cannot be both — every re-detection would overwrite
the evidence that justified what was said.

So three tables:

```
Insight             who this is    — one row per condition, forever
InsightObservation  what was seen  — one immutable row per run
InsightEvent        what happened  — one immutable row per transition
```

Current status is derived from the latest event; severity, confidence and
last-seen from the latest observation. **Nothing is mirrored onto the identity**,
because a mirror is a second truth that drifts.

`InsightRun` records the request and the ledger cutoffs it read at.
`InsightDetectorOutcome` records, per detector and **independently**, whether
execution finished (`SUCCEEDED` / `SKIPPED` / `FAILED`) and how much of the
population it could see (`COMPLETE` / `PARTIAL` / `INSUFFICIENT`).
`SUCCEEDED` + `PARTIAL` is the ordinary case for analytics over incomplete
operational data — and the combination that may show findings but must never
resolve one.

### Immutability

Migration `insights/0002_insight_append_only` installs the repository's standard
trigger pattern (`core/0002_auditevent_append_only`): `Insight`,
`InsightObservation`, `InsightEvent`, `InsightDetectorOutcome` and
`DetectorSetting` refuse `UPDATE` and `DELETE` outright.

`InsightRun` is the one exception and uses an **allowlist** trigger — only
`finished_at` and `updated_at` may change. Per `CLAUDE.md`: a blocklist has to
be remembered, and `accounting/0005` records what forgetting one column cost.

---

## 3. The detector

**Population.** Each stock-tracked item at each kitchen warehouse over a
half-open business-date window `[start, end)`.

**Formula.**

```
item_issue_ratio = actual_consumption / theoretical_consumption
```

- `actual_consumption` = `ItemFlow.total_consumption` from
  `apps/kitchen/consumption.py` — already a **positive magnitude**, because the
  engine negates the ledger's signed outbound quantities inside that property.
  **No `abs()`**: taking the absolute value of a magnitude is a no-op that would
  hide the day the engine's convention changed.
- `theoretical_consumption` = the sum of `EquivalentTotal.effective_base_quantity`
  across `REGISTERED_SOURCES` (SALES, STAFF_MEAL, COMPLIMENTARY_MEAL). Summing
  *across* sources is correct here and is not the double count `totals_by_item`
  refuses: those populations are disjoint, and a kilo consumed by any of them is
  a kilo the ledger should show leaving.

**Threshold.** `minimum_item_issue_ratio = "0.05"`, stored and compared as an
exact `Decimal`. The boundary is **strictly less-than**: equality with `0.05` is
not a finding.

**Severity.** `HIGH` when recorded consumption is exactly zero; `MEDIUM` when
non-zero but below the threshold.

**Fingerprint.**
`inventory-issue-coverage-gap:item=<id>:branch=<id|org>:warehouse=<id>` — stable
identity only. No period, no severity, no measured ratio, no translated label;
any of those would fork the case every week and nothing acknowledged would stay
acknowledged.

### Eligibility — where it refuses to speak

An item is compared only when, in the same window and scope: a posted receipt
exists; theoretical consumption is `> 0`; both sides share a base unit; and the
warehouse's stock identity holds (`identity_difference == 0`). Anything else is
an **exclusion recorded on the outcome**, never a guessed finding. Exclusions
degrade coverage to `PARTIAL`; an empty evaluated set is `INSUFFICIENT`, not a
clean `COMPLETE`.

### Wording

The narrative states that recorded consumption is missing or materially below
theoretical **despite posted receipts**, and that analyses depending on the same
inputs are therefore under-covered for that item. It never claims waste, loss,
theft or negligence — the quantity is missing, so any claim about where it went
would be invented. The recommendation asks an authorised manager to review the
issue-recording workflow and states explicitly that it assumes nobody's
responsibility.

---

## 4. Reused engines — nothing reimplemented

| Need | Reused |
|---|---|
| Actual consumption, buckets, identity proof | `apps/kitchen/consumption.py` → `kitchen_warehouse_flow`, `ItemFlow`, `MovementBucket` |
| Theoretical consumption | `apps/kitchen/consumption_sources.py` registry + `apps/sales/consumption_source.py` |
| Audit trail | `apps/core/services.record_audit_event` |
| Urgent delivery | `apps/core/automation.open_exception` — targets the stable `Insight`, carries sensitivity unchanged |
| Authorization | `apps/organizations/authorization` (ADR-016) |
| Safe Arabic console | `apps/core/console.SeedCommand` |
| Navigation gating | `apps/core/navigation_access` |

**Window translation:** the kitchen engines take *inclusive* date windows; ours
is half-open. The detector passes `period_end - 1 day`. Without that, one extra
business day of sales would be counted against the same stock.

---

## 5. Authorization

Four permissions: `view_insight`, `manage_insight`, `run_insights`,
`configure_insights` (service/model boundary only; no UI this stage).

Role defaults — owner: all four · manager and accounting manager: view, manage,
run · accountant and purchasing: view · storekeeper, cashier, viewer: none.
A storekeeper is absent deliberately: this detector is about *their own*
recording discipline and the conversation it starts is managerial. The roles
screen can grant it to anybody.

`selectors.visible_insights` is the single filtered authority for every surface
— dashboard counts, list, detail, htmx fragments and lifecycle actions. Two
querysets over one concept eventually disagree, and a count exceeding what the
list can show is itself a disclosure. Out of scope is **404**, never 403.

`readable_sensitivities` already gates `FINANCIAL` and `HR_RESTRICTED` even
though Stage 1 ships only `OPERATIONAL` findings, so later detectors inherit a
gate rather than inventing one.

---

## 6. Performance

No analysis runs during a page GET; the screens read rows the command wrote.
`visible_insights` annotates status and latest-observation fields in one
queryset — a per-row "current status" lookup over fifty findings would be a
hundred queries. Evidence lists are bounded (`MAX_SAMPLE = 20`) with explicit
truncation metadata. `lock_insights_run` is a transaction-scoped advisory lock
keyed per organization, deliberately **not** the account-mapping lock, which
every posting takes in shared mode.

---

## 7. Explicit deferral: the «تشغيل الآن» button

Not shipped. The scan reads every posted movement in the window across every
kitchen warehouse; doing that inside an HTTP request would hold a worker for as
long as the data takes, and the first busy month would turn the button into a
timeout. The repository has no approved background-job mechanism, and adding one
is out of scope. Scheduled execution is the platform's cron calling the same
service.

---

## 8. Known conflict found in an upstream engine — reported, not fixed

`apps/kitchen/consumption_reconciliation.py::usage_variance_analysis`
constructs `UsageVarianceAnalysis` and `UsageDiagnosticRow` **without passing**
`coverage_code`, `coverage_label` or `finality_label`, so all three always fall
back to their frozen defaults `SALES_NOT_INCLUDED` / `PARTIAL_COVERAGE` /
`NOT_FINAL_USAGE_VARIANCE`. Meanwhile the nested `coverage: TheoreticalCoverage`
**is** computed live and reports `is_final=True`, because the sales adapter is
registered at app-ready.

The two therefore contradict each other inside one object:
`analysis.coverage.is_final == True` while
`analysis.finality_label == "NOT_FINAL_USAGE_VARIANCE"`.

**Impact on this stage:** none, because the detector reads the consumption
engines directly and never consults those labels. It is recorded here because a
later stage that trusted `finality_label` would conclude coverage is never final
and suppress every finding. Fixing it belongs to the kitchen module and was left
alone: the diff must contain only this stage.

---

## 9. Validation

Commands run from the repository virtual environment:

```
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe -m pytest apps/insights/tests/
.\.venv\Scripts\ruff.exe check apps/insights/
.\.venv\Scripts\ruff.exe format --check apps/insights/
.\.venv\Scripts\mypy.exe apps/insights/
```

Results are recorded in the delivery message accompanying this report rather
than asserted here, so this document never claims a result that was not seen.

### Real development-database run (read-only, rolled back)

A run over `2026-08-01 → 2026-09-01` for organization `01` produced:

```
inventory-issue-coverage-gap: SUCCEEDED · coverage COMPLETE · 17 candidates
items_seen 83 · items_evaluated 17
skipped_no_theoretical_consumption 54 · skipped_no_posted_receipt 12
skipped_unit_mismatch 0 · skipped_identity_unreliable 0
```

Sample findings (real data, transaction rolled back — nothing persisted):

| Severity | Item | Actual | Theoretical | Ratio |
|---|---|---|---|---|
| MEDIUM | لحم لشه (طلي) | 1.000000 | 877.000000 | 0.001140 |
| HIGH | تمن مندي | 0 | 1387.666796 | 0 |
| HIGH | ملح المنصور | 0 | 30.532936 | 0 |

The 54 items excluded for absent theoretical consumption are the ones whose
recipes are still empty drafts — the same 39 draft recipes recorded elsewhere.
The detector reported that as an exclusion rather than as a finding, which is
the behaviour §3 requires.

**This is a development observation, not a test result.** The implementation
does not depend on it and the tests do not reference it.
