# ADR-035 — Transactional automation foundation

## Status

Accepted for the first automation batch — 26 August 2026.

## Context

Khan Mandi RMS had no durable job queue, retry policy, dead-letter record, or
role-scoped task inbox.  A successful web request could therefore leave a
notification or reconciliation check unperformed, while a failed integration
would require someone to discover the failure by polling screens.

The system already relies on PostgreSQL transactions, immutable financial
documents, exact decimals, scoped roles, and an append-only audit trail.  The
automation boundary must preserve those properties.  In particular, a worker
must not be another route to approve a payment, post a journal, change stock,
or override a close exception.

## Decision

1. Use a PostgreSQL transactional outbox (`AutomationOutboxEvent`) for every
   implemented automated follow-up.  A domain service inserts its event inside
   the same `transaction.atomic()` block as the originating change.
2. Process messages with the `process_automation_outbox` management command.
   Workers claim one due row with `select_for_update(skip_locked=True)`, write
   an attempt row, and run an idempotent handler.  A crashed claim is recovered
   after a bounded timeout; failures back off exponentially and become a
   visible dead letter after five attempts.
3. Require an organization, optional matching branch, correlation ID, source
   identity, idempotency key, safe payload hash, actor/system identity, and
   attempt history.  PostgreSQL triggers reject branch/organization mismatch
   even when a caller bypasses the service layer.
4. Represent current findings as `AutomationException` and the human work as
   a deduplicated `AutomationTask`.  Resolving the underlying condition
   resolves its active task but never changes the historical source document.
5. Keep task acknowledgement separate from domain approval.  Replaying a
   dead-letter event requires `core.replay_automation_outbox` over the owning
   organization; it does not grant any financial authority.
6. Do not choose email, SMS, push, queue broker, malware scanner, or external
   integration provider in this decision.  In-app tasks and structured worker
   logs are the active delivery channel until an owner approves providers,
   secrets, data-processing terms, retention, and escalation SLAs.

## Consequences

- A retry can repeat an event safely only when its handler is idempotent.
  Handlers therefore create or refresh exceptions/tasks by deterministic keys
  rather than emitting a new alert on every run.
- The deployed command is intentionally a worker entry point, not a scheduler.
  Production operations must arrange a supervised recurring invocation or a
  long-running worker before treating automation as time-critical.
- Event payloads are restricted to small, JSON-safe references and facts;
  passwords, tokens, credentials, raw files, and document bytes are rejected.
- The existing import framework remains the source-batch implementation for
  inventory and procurement.  Sales/app/card/bank adapters will register on
  the foundation only after their source contracts and retention policy are
  approved.

## Rejected alternatives

- **Django signals or `transaction.on_commit()` alone:** neither supplies a
  durable retry record or a visible dead-letter workflow.
- **Direct synchronous email/SMS from a posting service:** an upstream outage
  would turn a user action into an unreliable integration transaction.
- **Automatic posting/approval by a handler:** violates maker-checker and
  creates a second unreviewed financial mutation path.
