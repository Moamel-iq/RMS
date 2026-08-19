# ADR-029 — Accounting operations, manual journals, and the read-only subledger workspaces

- **Status**: Accepted
- **Date**: 2026-08-19
- **Task**: 5.0 — Accounting domain specification, and Phase 5 checkpoints 1–4
- **Related**: ADR-013 (fiscal year and periods), ADR-014 (chart of accounts),
  ADR-016 (permission plus scope), ADR-017 (source identity and idempotency),
  ADR-019 (account roles and domain-owned posting mappings), ADR-023 (GRNI
  clearing), ADR-027 / ADR-028 (sales recognition and settlements)
- **Companions**: ADR-030 (cash, bank, expenses and deferrals), ADR-031
  (financial statements and year-end close)

---

## 1. Context

The ledger has been posted into by four modules for four phases and read by
nobody. Every journal in the database was written by a source document, and the
only way to see one was `manage.py shell`.

Phase 5 opens it. That raises three questions the earlier phases never had to
answer, because they never had a screen that could write directly into the
ledger:

1. Who may write a journal by hand, and what stops that from becoming a way
   around every control the source modules enforce?
2. What is a supplier balance, now that two different things could legitimately
   claim to be one — the Procurement documents, and the GL control account?
3. What may an Accounting screen do to a record another module owns?

---

## 2. A manual journal is the exception, not the general case

**Decision.** A manual journal is for what has no source document: a
correction, a reclassification, an opening balance, an adjustment an auditor
asked for. It is never the way to record a purchase, a sale, a production run
or a stock movement — each of those has a document that posts its own journal
with its own controls, and a manual duplicate would be a second version of the
same economic event that no reconciliation could distinguish from a real one.

Three mechanisms hold that line.

**Maker-checker.** `JournalEntry` gains `created_by`. A manual journal's
creator may not post it. The kernel already recorded `posted_by`; it recorded
no creator at all, so "the same person entered and posted this" was not a
question the database could answer. It is now.

System-generated journals are **exempt** — deliberately. A supplier invoice
already cannot be posted by the person who entered it; Procurement enforces
that at its own boundary, on the document, where the segregation actually
means something. Re-checking it on the journal would either duplicate that rule
or, worse, contradict it.

**Account policy.** `Account.manual_posting_policy` is `ALLOWED`, `RESTRICTED`
or `FORBIDDEN`.

The interesting value is `RESTRICTED`, and it is seeded onto the control
accounts a subledger owns: supplier payable, delivery-app receivable, inventory
control, GRNI. A manual credit to supplier payable is not an accounting error —
it balances, it posts, the trial balance still ties. It silently breaks the
equality that `ذمم الموردين` exists to prove, and the workspace then reports a
discrepancy whose cause is invisible from the subledger side. `RESTRICTED`
means an accountant who genuinely needs that entry can still make it, holding
`post_restricted_manual_journal`, and the workspace can name the entry as the
reason the two sides disagree.

**Checked twice.** Policy is validated when the draft is shaped and again when
it is posted, because the policy can change between the two and the draft may
sit for a week.

## 3. Consequences

A ledger with a genuine segregation of duties on hand-written entries, and a
named, auditable path for the small number of manual entries that must touch a
control account. The cost is one extra column, one extra permission, and a
draft that can become invalid while it sits — which is correct, and which the
screen reports rather than swallowing.

---

## 4. A subledger balance is derived, and the workspace proves the two agree

**Decision.** `ذمم الموردين` and `ذمم التطبيقات` create **no model of their
own**. Both are read workspaces that compute two numbers from two independent
sources and put them side by side.

For suppliers: the subledger side sums Procurement's posted invoices, credit
notes, payments, allocations and returns. The GL side is the balance of the
account that carries the `SUPPLIER_PAYABLE` role on the as-of date. For
applications: the subledger side sums Sales's append-only
`ApplicationReceivableEntry` rows; the GL side is the
`DELIVERY_APP_RECEIVABLE` account.

**The rejected alternative was a `SupplierBalance` table** maintained by
triggers or signals as documents post. It is faster to read and it is how most
systems do it, and it has one property that disqualifies it here: when it
drifts, both sides of the reconciliation come from the same drifted number, so
the reconciliation reports agreement. A balance that can disagree with its own
movements is precisely the failure this architecture was built to prevent —
the same reasoning ADR-018 records for stock value and ADR-027 for the
application receivable.

**A discrepancy is reported and never repaired.** No screen offers to "fix"
the difference. The workspace names the amount, the organization, the supplier
or application, the branch and the date, and the accountant finds the journal.
An automatic repair would post a plug entry that makes the two sides agree
while making the books wrong, and would do it without anyone reading the
difference that would have explained the cause.

## 5. Consequences

Reads are more expensive — an aging report is an aggregate over documents
rather than a table scan. That is the correct trade at this scale: the
alternative is a number that can lie.

Accounting must import Procurement's and Sales's read surfaces. The direction
is one-way and stays that way: **no source module imports an Accounting
workspace**, and no Accounting service calls `save()` on a source-domain model.
`apps/accounting` reads their selectors; it never reaches into their services.

---

## 6. Accounting may read a source record, and may never rewrite one

**Decision.** An Accounting screen that displays a supplier invoice offers no
way to change it. The invoice belongs to Procurement, its lifecycle is
Procurement's, and a second edit path would mean two sets of validation rules
over one document — of which the weaker one is the one that matters, because
that is the one an operator will find.

Where an Accounting workspace needs a change to a source document, it links to
the owning module's screen. The reconciliation reports what is wrong; the
module that owns the record is where it is put right.

---

## 7. Scope, unchanged

Permission **plus** scope, as ADR-016 has required since Phase 0. Out of scope
is 404 and in scope without authority is 403, for the reason ADR-016 records: a
403 about another organization's record confirms it exists, and ids are
sequential.

The Task 1.3 accounting views checked `user.has_perm(...)` alone — a global
answer to a question that is always local. Phase 5 moves every Accounting view
onto the scoped helpers. That is a **tightening**: a user who could previously
reach the mapping screen through a permission held in some other organization
now cannot.

Structural acts — chart, mappings, periods, year end, statements — are
organization-scoped, because each affects every branch at once and one branch
must not reshape what the others post to. Acts that name a branch — journal
lines, expense vouchers — are branch-scoped.
