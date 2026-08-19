# Accounting reports — the four statements, both subledgers, and the verifier

**Status:** current as of Phase 5 completion, 2026-08-19.
**Decisions:** ADR-031 (financial statements and year-end close), ADR-029 §2–§3
(the two reconciliation workspaces). Where this document and an ADR disagree,
the ADR wins and this document is wrong.

Everything here is **read-only**. There is no repair control on any of these
screens and no repair endpoint in any of these routers. Where a report
disagrees with itself it says by how much and stops — a plug entry that made
the two sides agree would make the books wrong and would do it without anybody
reading the difference that explained the cause.

---

## Where a report's numbers come from

`POSTED` **and** `REVERSED` journal lines. A reversal is itself posted and its
original stays in the ledger, so both belong in a balance and the pair nets to
zero. Excluding reversed entries would make a reversal look like a deletion,
which is the one thing the append-only ledger exists to prevent.

Statement order is one tuple, named once in `apps/accounting/statements.py` and
used everywhere:

```
entry__accounting_date → entry__posted_at → entry__entry_number → line_number
```

A running balance is accumulated **in the service**, never in a template. A
template cannot carry a Decimal across rows without a filter that hides the
ordering assumption — and the ordering is the entire content of the column.

---

## ميزان المراجعة — trial balance

Opening, period movement and closing, per account, with both totals and the
difference between them. The difference is displayed whether or not it is zero.

`include_zero` exists because an account that moved and netted to zero is
sometimes exactly what a reader is looking for, and hiding it by default is a
presentation choice rather than a truth about the ledger.

---

## دفتر الأستاذ — general ledger

One account's movement in statement order with a running balance, filterable by
branch, cost centre, date, source document type and origin.

`source_type` is upper-cased on the way in, because `canonical_source_identity`
stores it that way (ADR-017). A caller passing the natural spelling would
otherwise filter for a string the ledger does not contain and receive an empty
report that looks like an answer.

`source_document_id` crosses the API as a **string**. Upstream documents
identify themselves by UUID as often as by primary key; typed as an integer it
returned a 500 the first time a Sales journal reached it.

---

## قائمة الدخل · الميزانية العمومية — the statements

Both are driven by `AccountReportMapping`: an explicit classification of each
account into a statement group, with a closed set of groups. The account
**class** is not the classifier — class 7 is "other income *and* expense" and
class 1 does not distinguish current from non-current, so a class-driven
statement would put items in the wrong section and look right doing it.

### The unmapped rule

**An account with a balance and no statement group appears in غير مصنّف and
blocks approval.** It is never silently omitted.

This matters more than it first looks. The statement services resolve their
account set **from the ledger**, not from the mapping table, so an unmapped
balance cannot vanish. Had they read the mapping table instead, an unmapped
account would simply not appear — and the statement would still tie, which is
what makes the omission dangerous: nothing downstream would look wrong.

This rule is why the demo seeds a statement group for every postable account.
The first verifier run reported **27 unclassified balances** and both
statements refused approval. That was the rule working, not a defect.

### Current-year earnings are computed

Equity carries a computed «أرباح السنة الحالية» line before year-end close, so
the accounting equation holds on any date without the income-statement accounts
being physically closed every month. Monthly closing entries would destroy the
year-to-date income statement (ADR-031 §3).

`is_approvable` is `is_balanced and not unmapped`, on both statements. The API
returns all three so a client never has to re-derive the rule.

---

## ذمم الموردين · ذمم التطبيقات — the two workspaces

Read and reconciliation only. Neither has a model of its own, and that is
deliberate — the permissions `view_supplier_liabilities` and
`view_application_receivables` are declared on `AccountingSettings` because
there is no table to hang them on.

**They forward rather than re-derive.** The supplier workspace calls
Procurement's `supplier_aging`; the application workspace calls Sales'
`positions_for`. A second derivation agrees with the first right up until the
day it does not, and then there are two numbers and no way to tell which is
wrong.

The supplier comparison uses **`net_position`** (invoices − credit notes −
allocated payments), not `open_total`. `open_total` excludes standing credit
and advances, so it would never tie to the GL control account and the workspace
would report a permanent phantom difference.

Forbidden here, permanently: a `SupplierBalance` table, a mutable supplier
outstanding, a second allocation model, a mutable application balance. A
discrepancy is **reported and never repaired automatically** (ADR-029 §3).

---

## CSV exports

Every report exports the rows **the screen just built**, from the same service
call — not a second query. Two query paths drift, and the CSV is the one nobody
looks at until an auditor does.

Exports are UTF-8 with a BOM (Excel needs it to read Arabic), amounts use
`money_export` (3 dp, ungrouped, locale-independent), and every cell goes
through `apps/inventory/report_views.neutralise` so a value beginning `=`, `+`,
`-` or `@` cannot execute as a formula in a spreadsheet.

---

## `verify_accounting`

```bash
.venv\Scripts\python.exe manage.py verify_accounting --organization DEMO-KHAN-MANDI
```

Sixteen checks plus the stored-balance check, answering one question: **does
everything the Accounting module shows still agree with the ledger underneath
it?**

Three severities, one of which is a failure:

| Severity | Meaning | Exit code |
|---|---|---|
| `ERROR` | a real disagreement | 1 |
| `ADVISORY` | worth a human's attention | unchanged |
| `COVERAGE_LIMITATION` | something is knowably absent | unchanged |

A check that *raises* is reported as a finding of its own rather than aborting
the run — the other fifteen answers are still worth having.

**Report-only.** No `--fix`, no `--repair`, no `--rebuild`. A verifier that
could change what it verifies is one nobody can trust, and the single situation
where a repair is tempting — the numbers disagree — is exactly the one where a
human has to see them disagree first.

It composes rather than repeats: `verify_supplier_payables` belongs to
Procurement and `verify_receivable_ledger` to Sales, and both are forwarded.

### What it checks

| Check | What a finding means |
|---|---|
| journals balance | a posted entry's debits and credits differ |
| source identity | an upstream journal's identity is incomplete or names no document |
| manual maker-checker | a manual entry was posted by its own author |
| account hierarchy | a postable account has postable children, or an orphan parent |
| mapping continuity | a role has a gap or an overlap in its effective dates |
| cash account consistency | two live cash records name one GL account |
| supplier subledger | the GL control account and `supplier_aging` disagree |
| application subledger | the GL control account and the receivable ledger disagree |
| expense vouchers | a posted voucher's journal does not match its lines |
| accruals | a posted accrual carries no journal, or a reversed one still stands |
| prepayment schedules | a schedule does not sum to its header |
| trial balance | the ledger as a whole does not tie |
| statement mapping | an account with a balance carries no statement group |
| income statement | the statement is not approvable |
| balance sheet | assets ≠ liabilities + equity |
| periods | a closed period contains a draft, or the sequence is out of order |
| stored balance | a balance column has appeared on a model that must not have one |

The last one is about the **code** rather than one organization's data, so it
runs once per invocation. It is the tripwire for the rule the whole module
rests on.
