# ADR-036 — POS/KDS and delivery boundary

## Status

Decision deferred pending owner and operations evidence — 26 August 2026.

## Context

The current Sales domain is a back-office daily-sales ERP.  It has no durable
order identity, waiter/table lifecycle, device/offline model, KDS routing, or
driver/COD wallet.  Building reservations, loyalty, direct delivery, or a
transaction POS before deciding these boundaries would duplicate identities
and create accounting records that cannot be traced to the kitchen or payment
source.

## Decision

Do **not** build an internal POS/KDS, delivery dispatch, COD wallet,
reservations, or loyalty in the first automation batch.  Use an
integration-first assessment before an internal product decision.

The owner and operations lead must provide:

1. number of branches, tills, waiters, kitchen/bar stations, printers and
   card terminals;
2. operating hours, offline tolerance, supported devices and network failure
   procedure;
3. Iraqi fiscal-receipt and tax requirements confirmed by the tax adviser;
4. every delivery application, payment acquirer, and direct-delivery channel;
5. whether direct delivery exists, with driver employment/contractor model,
   COD ownership, proof-of-delivery and cash-remittance process;
6. an integration contract or evaluated provider API for each external source.

When evidence is available, select either an external POS/integration or an
internal MVP through a follow-up ADR.  Any internal design must use append-only
order/payment/discount/void/refund events, offline idempotency, immutable
modifier/price snapshots, and automatic-but-reviewable SalesDay aggregation.

## Consequences

- The new outbox, import, task, and reconciliation foundation can support
  either decision without embedding provider credentials or device logic now.
- Delivery-app statement reconciliation remains in scope once an approved
  statement/API contract is supplied.  Driver/COD controls remain out of scope
  unless direct delivery is confirmed.
