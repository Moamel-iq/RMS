# Automation foundation — dependency-aware rollout

## Purpose and current boundary

This plan turns the audit's automation backlog into reliable, scoped work. It
automates collection, matching, draft creation and exception ownership; it
does **not** auto-approve supplier payments, refunds, payroll, write-offs,
journals, stock adjustments, or any other high-risk financial action.

The current data is a regression scenario, not proof of loss. In particular,
IQD 35,566,550 of application sales is undeclared, no cashier close exists for
the posted SalesDay, 131 sales lines lack cost, 100 inventory items lack a
balance, and the IQD 27,000,000 salary expense is unsupported by HR/payroll
records. Nothing in this migration invents evidence or edits those historical
facts.

## First safe batch — implemented

| Capability | Models and services | Control and UI | Job/metric/test evidence |
| --- | --- | --- | --- |
| Transactional outbox | `AutomationOutboxEvent`, `AutomationOutboxAttempt`; `enqueue_event`, claim, retry, stale-claim recovery, dead-letter replay | Organization/branch pair guarded in PostgreSQL; payload hash, correlation ID, source identity and idempotency key; replay permission is limited to owner/finance control roles | `process_automation_outbox --limit 100`; queue depth, oldest pending age, retries, dead letters and processing duration projection; rollback, duplicate, concurrent-claim, retry/dead-letter tests |
| Task inbox and persistent exceptions | `AutomationException`, `AutomationTask`; open/refresh/resolve and acknowledgement services | Role-, organization-, branch- and HR-sensitivity filtering; one active task per open condition; acknowledgement is not approval | `/settings/tasks/` supports HTMX acknowledgement; tests cover tenant/role isolation and task acknowledgement |
| Daily-close exception ownership | Sales close submission enqueues `sales.daily_financial_close.captured`; the Sales handler materializes tender, cash and cashier-close findings | Findings remain blocking in the immutable close; a clean later capture resolves only the current exception/task, never the historical close attempt | Tests cover a declared-vs-derived tender mismatch and missing cashier close becoming one blocking exception and task |
| Import foundation assessment | Existing `inventory.ImportBatch`/`ImportRowResult` already provides validated preview, hash, row results, idempotent application and procurement registrations | It is deliberately limited to master data/drafts and preserves separation of import, validation and application | Sales, delivery-app, acquirer, bank and payroll source adapters remain next work; no duplicate import framework was introduced |

Migrations: `core.0007_automation_foundation` creates the shared tables and
permissions; `core.0008_automation_branch_scope_guard` adds the PostgreSQL
organization/branch guard. Existing financial and posted records are unchanged.

## Dependency map

| Sequence | Feature | Dependencies | Planned outcome | Decision still needed |
| --- | --- | --- | --- | --- |
| 1 | Shared outbox, exceptions and inbox | PostgreSQL, scoped roles, audit context | **Implemented** | Worker deployment schedule and alert routing |
| 2 | Immutable source imports | Existing import batches + protected evidence | POS/app/card/bank/payroll batch adapters, row-source links and rejection retry | Provider contracts, retention, malware scan/storage |
| 3 | Daily-close match engine | Source imports, cash/till/terminal identity | Deterministic application/card/bank matching and suggested ambiguous matches | Match tolerances and override/evidence policy |
| 4 | Protected evidence | Storage, retention, permission-checked download | Versioned document hashes and conditional evidence rules | Storage/signing/scanning provider and retention/legal policy |
| 5 | Role recertification | Employee↔user relation, membership model, task inbox | Quarterly scoped certifications and toxic-access findings | Inactivity threshold, auto-revoke policy, HR status ownership |
| 6 | FEFO and reorder drafts | Lot/expiry data, usage history, catalogue/PO data | Suggestions/warnings, draft PR only | Waste/usage forecast and emergency threshold policy |
| 7 | Delivery and card settlement | Import batches, agreements, real bank/cashbox masters | Clearing aging, deterministic matching and dispute tasks | Acquirer/application statement schemas and bank selection |
| 8 | Cost/margin and subledger/GL reconciliation | Cost snapshots, complete mappings, payroll source | Explicit COGS/payroll/AP/inventory exceptions and period blockers | Materiality tolerances and accountant sign-off rules |
| 9 | Approval reminders/anomalies | Task inbox and stable status transitions | Deduped, explainable reminder and investigation tasks | Escalation recipients, ages and value thresholds |
| 10 | POS/KDS, direct delivery, customer features | ADR-036 decision and reliable order identity | External integration or internal order MVP | Owner/operations/tax/device decision |

## Current data: before and after

| Audit scenario | Before this batch | After this batch | What remains human-controlled |
| --- | --- | --- | --- |
| IQD 35,566,550 delivery-application mismatch | A report calculated a gap but there was no persistent owner | Any new controlled application-tender mismatch becomes a blocking SalesDay exception and a finance task from the immutable close snapshot | Importing the real application statement, confirming the match and approving/posting settlement |
| Missing cashier close | A report limitation with no owner | Any new controlled close records `cashier_shift_missing` as a blocking exception/task | Cashier count, independent reviewer and SalesDay posting |
| 131 uncosted sales lines | Zero COGS could be misunderstood as profit | Documented as an explicit future cost-coverage exception; no costs were fabricated | Recipe/cost snapshot creation, finance review and COGS policy |
| Stock gaps / cancelled counts | Empty reports and no recommendation ownership | Remain untreated to avoid inventing usage, expiry or reorder policy | Approved opening/baseline counts and item classification |
| Salary expense without HR/payroll | Unsupported salary balance had no scheduled reconciliation record | Remains a documented reconciliation requirement; no payroll was inferred | HR employee/payroll evidence and accountant review |

The daily-close enforcement date remains the deployment date held in
`AccountingSettings.daily_close_enforced_from`. The batch deliberately does
not backfill current posted history or create retroactive tasks for evidence
that did not exist at the time.

## Operations and rollback

Run a supervised worker or scheduler with the project environment:

```powershell
.\.venv\Scripts\python.exe manage.py process_automation_outbox --limit 100
```

Multiple workers are supported: row locking prevents two workers from claiming
the same message. Monitor the command's structured log fields and the
**مراقبة الأتمتة** screen for pending age, retries and dead letters. A dead
letter can be replayed only after an authorized owner/finance controller has
corrected the underlying issue; replay reuses the same idempotency key and does
not alter any source document.

`core.0007` is schema-additive and `core.0008` adds guards. Reversing
`core.0007` would drop outbox/task evidence, so it is **not** an acceptable
production rollback after messages exist. Take and verify a database backup;
disable the worker and deploy a forward corrective migration instead. Rolling
back `core.0008` alone only removes the guard and is likewise not a control
remedy.

## Decisions and owners required next

1. **Owner + operations:** POS/KDS, direct delivery and COD scope (ADR-036).
2. **Owner + finance controller:** daily-close, settlement and reconciliation
   materiality thresholds; exception due dates and escalation recipients.
3. **Owner + legal/security:** evidence retention classes, storage region,
   signed-download method, malware-scanning provider and HR privacy policy.
4. **Owner + technology:** production worker supervision, metrics/alert
   receiver, email/SMS/push provider, secret management and delivery retries.
5. **Finance + integration owners:** verified CSV/API schemas, immutable
   source identifiers and agreements for every delivery app, card acquirer,
   bank and payroll source.
6. **HR + security owner:** employee↔user lifecycle source, recertification
   cadence, toxic-access list and any pre-approved automatic revocation rule.

## Acceptance evidence to retain

- `apps/core/tests/test_automation_foundation.py` covers transaction rollback,
  event idempotency/conflict, handler retry idempotency, concurrent claiming,
  bounded retry/dead letter, authorized replay, task scope and database tenant
  guard.
- `apps/sales/tests/test_cashier_shifts.py` covers daily-close mismatch and
  missing cashier-close task materialization while retaining the existing
  maker-checker and posting gates.
- `manage.py check`, migration state and lint should pass in the deployment
  environment before enabling a scheduler.
