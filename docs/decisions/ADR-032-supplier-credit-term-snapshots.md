# ADR-032 — Effective-dated supplier credit terms and invoice snapshots

- **Status:** Accepted
- **Date:** 2026-08-19
- **Scope:** Procurement completion, capability 3
- **Related:** ADR-012 (precision and allocation), ADR-016 (permission and
  scope), ADR-017 (source identity and retries)

## Context

`Supplier.payment_terms_days` and the invoice/order integer snapshots existed
before this decision. That was enough to preserve an already-created
document's due date, but it could not answer who changed the agreement, when it
became effective, which version an approver accepted, or what term applied to a
late-entered historical invoice. Making both the supplier integer and a new
version table editable would create two answers to the same question.

## Decision

`SupplierCreditTerm` is the source of truth. It is organization- and
supplier-scoped, versioned, effective-dated, audited, and has the lifecycle
`DRAFT → ACTIVE → SUPERSEDED`.

The rules are enforced at more than the form boundary:

- inclusive effective ranges for active versions cannot overlap;
- one supplier may have only one draft;
- the creator cannot activate their draft;
- activation locks the supplier, draft, predecessor and conflicting ranges;
- an activated row is immutable, except that replacement may close it and
  mark it superseded;
- a superseded row and every activated row cannot be hard-deleted;
- correction always creates another version.

`SUPERSEDED` does not mean historically ineffective. Its closed range remains
authoritative when resolving an old invoice date. The current version and a
closed predecessor therefore form one continuous effective-dated history.

## Invoice decision boundary

Approval—not draft entry—is the decision boundary. Approval resolves the term
covering the supplier invoice date and freezes all of these on the invoice:

- credit-term public UUID;
- version;
- display name;
- net days;
- due date (`invoice_date + net_days`).

Later activation, correction, or supplier maintenance cannot rewrite an
approved, posted, or reversed invoice. The existing invoice immutability
trigger protects the new snapshot columns with every other financial field.

## Compatibility projection

`Supplier.payment_terms_days` remains because existing integrations and
purchase-order creation read it. It is a compatibility projection written by
initial supplier creation and credit-term activation, not an independently
editable agreement. Supplier edit forms, the supplier update API, and
`update_supplier` cannot change it.

Initial supplier creation atomically creates version 1 from the supplied
initial days. The data migration does the same for pre-existing suppliers and
builds exact term versions for any different days already frozen on legacy
invoices. Legacy activation has no invented human approver; its audit metadata
identifies it as a system bootstrap.

## Migration and database enforcement

The migration is additive and backfills every current and historical invoice.
It temporarily suspends the pre-existing invoice immutability trigger only
inside the migration transaction, writes the new snapshot columns, and
restores the identical trigger before committing. PostgreSQL then owns the
inclusive-range exclusion and activated-row immutability guards.

## Consequences

Supplier terms are reviewable and reproducible at any date. Invoice aging reads
the approved due date rather than today's supplier record. UI and API share the
same services and maker-checker commands, and foreign identifiers remain 404
while an in-scope actor without activation authority receives 403.

The rejected alternatives were a mutable supplier integer, recalculating due
dates during reporting, and editing an activated term in place. Each would
silently restate history.
