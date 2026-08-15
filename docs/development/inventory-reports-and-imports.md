# Inventory reports, exports, imports and projection verification

Task 1.7A. What each piece does, what it refuses to do, and the two questions
an operator has to be able to tell apart.

## The two historical modes

This is the one thing to read before using a dated report.

A movement carries two times, and they answer different questions:

| | |
|---|---|
| `posted_at` / `posted_sequence` | when the system learned it |
| `effective_at` | which business moment it belongs to |

They diverge whenever somebody posts late, which in a restaurant is most
Mondays. A delivery that physically arrived on the 28th and was keyed in on the
2nd carries `effective_at` = 28th and a posting sequence from the 2nd.

**`POSTED_AS_OF` — "what did the books say at that moment?"**
The audit answer, and the default. Reproduces a figure somebody printed last
month. It takes a prefix of the posting order, which is exactly what the
valuation kernel replayed, so the stored `quantity_after`, `value_after` and
`average_after` of the last included movement *are* the position. Nothing is
recomputed.

**`EFFECTIVE_DATE` — "which movements belong to that business period, knowing
what we know now?"**
The management answer. Its movement set is **not** a prefix of the posting
order — the late-keyed delivery sits inside it while later-effective,
earlier-posted movements sit outside — so the stored running totals do not
describe it. Quantities and values are summed, because they are additive, and
the average is derived as value ÷ quantity.

### The two will disagree, and neither is wrong

If a month-end valuation printed on the 1st differs from the same window run
today in effective-date mode, that is the late-posted delivery, not corruption.
Use posted-as-of to reconcile against something already printed; use
effective-date to ask what a period really cost.

Every historical screen shows which mode produced it, and the mode travels into
the CSV. A report offering one unlabelled "as of" filter would silently pick
one and be wrong for half its readers.

**Posted movements are never repriced.** Both modes read `inventory_value` as
the kernel computed it.

## Reports

| Report | Route | Modes | Export |
|---|---|---|---|
| Stock valuation | `/inventory/reports/valuation/` | both | CSV |
| Stock card | `/inventory/reports/stock-card/` | both | CSV |
| In-transit and ageing | `/inventory/reports/in-transit/` | — | CSV |
| Expiry | `/inventory/reports/expiry/` | — | CSV |
| Reorder | `/inventory/reports/reorder/` | — | CSV |
| Waste summary | `/inventory/reports/waste/` | — | CSV |
| Count variance | `/inventory/reports/count-variance/` | — | CSV |
| Adjustments | `/inventory/reports/adjustments/` | — | CSV |
| Inventory-to-GL reconciliation | `/inventory/reconciliation/` | — | — |

Notes worth knowing:

- **Stock valuation** with no date window reads `StockBalance`, the projection
  the ledger maintains, because that is what "now" means. With a window it
  folds movements in the mode's own way.
- **Stock card** is always ordered by posting sequence, in both modes. Ordering
  by effective date would show running totals jumping backwards, because they
  were computed in posting order and no other.
- **In-transit ageing** measures from the dispatch business date, not from
  `created_at`. A transfer keyed in late was still in transit from the day it
  left.
- **Expiry** lists only positions holding stock. A lot emptied to zero has
  already been dealt with, and listing it forever would bury the ones that have
  not — the history stays visible on the waste report and the stock card.
- **Reorder** compares the branch's whole holding, summed across its
  warehouses, because the reorder point is a branch decision. It suggests
  nothing and orders nothing: there are no purchase orders in Phase 1.
- **Reconciliation has no repair button.** A difference is evidence that
  something posted wrongly, and a button that made it go away would destroy the
  only signal that it happened.

## Valuation redaction

Cost, value and average are **omitted** for a caller without
`inventory.view_valuation` — never blanked. An empty cell still says a number
belongs there.

The decision is made once, in the view, and passed to the report service, which
leaves the keys out of the row entirely. The template renders whatever keys
exist and the CSV writer writes whatever columns exist, so **the export cannot
be the way round the screen's redaction**. A storekeeper who exports a
valuation report gets a file with no cost column, not one with empty cost
cells.

## Export security

CSV only. XLSX was not added: it would mean a new dependency for a format this
release has no requirement for, and CSV preserves exact decimal text without
one.

Every export:

- runs the **same** scoped service with the **same** filters as its screen;
- carries a provenance block — report name, generation time, active mode, and
  the filters applied;
- is UTF-8 **with a BOM**, because without it Excel on Windows opens Arabic as
  mojibake;
- renders Decimals through `format(value, "f")` — exact, unlocalised, never
  scientific notation, and never through `float`;
- prefixes any cell starting with `=`, `+`, `-`, `@`, tab or carriage return
  with a single quote, so a spreadsheet treats it as text. A file exported from
  here is opened on somebody else's machine;
- gets a filename built from a constant stem and a timestamp — no path, no
  separator, nothing the user typed.

## Imports

    UPLOADED → VALIDATED → APPLIED
    UPLOADED → FAILED_VALIDATION → CANCELLED

**Upload parses. Validation judges. Only apply writes.**

Preview is a separate step rather than a dry-run flag on purpose: a dry run
sharing a code path with the real thing is one `if` away from writing during
the preview, and the failure is silent. Validation here *cannot* write, because
it has nothing to write with — it produces row verdicts, and only `apply_batch`
knows how to turn a verdict into a record.

### Atomicity, and the 99-of-100 case

A batch with any invalid row cannot be applied. It does not apply the 99 and
report the 1.

A spreadsheet is one act of intent. Applying most of it leaves the operator
holding a file that is neither applied nor not, and the only way to find out
which rows landed is to read them back one at a time. Refusing is recoverable;
partial success is not. The preview shows all 100 rows either way, because
knowing *which* row is wrong is what makes it fixable.

### Supported kinds

| Kind | Column headers |
|---|---|
| Item categories | `code`, `name_ar`, `name_en?`, `parent_code?` |
| Package units | `code`, `name_ar`, `name_en?` |
| Branch item settings | `item_code`, `is_stocked`, `reorder_point?`, `reorder_quantity?` |

`is_stocked` accepts `yes/no`, `true/false`, `1/0`, `نعم/لا`.

Those three are the whole enum. Inventory items, item conversions,
warehouses and opening-stock drafts were declared during Task 1.7A and
**removed again** once it was clear none had an apply service — they are not
"unsupported values", they are absent. `ImportKind` has three members, the UI
offers three options, and a `CheckConstraint` refuses any other value at the
database, so a raw INSERT or a data migration cannot create a batch that could
never be previewed or applied.

Opening-stock import is deferred deliberately, and it is the one to argue
about: it is the only kind that would reach the ledger, and even as a draft it
sets the ledger's starting position. It earns its own review rather than a
corner of this task. The `inventory.import_opening_draft` permission stays
**reserved and granted to no role** while that is true — the same treatment
`override_negative_stock` gets, because a grant for a capability nobody can
exercise is a grant nobody audits.

**Nothing imports a posted movement.** There is no kind for a receipt, issue,
transfer, count, waste or adjustment, because there is no such path.

### Idempotency

`content_hash` fingerprints the normalised rows — sorted keys, canonical
separator — so the same content saved by two spreadsheets fingerprints the
same. A partial unique index allows one APPLIED batch per
`(organization, kind, content_hash)`, so re-applying the same file is
recognised as a retry and refused with `import_content_already_applied`, not a
raw `IntegrityError`. Re-*uploading* is fine; people lose tabs.

A row asking for a value the record already holds counts as `unchanged`, which
is why `applied_row_count` can be lower than `valid_row_count`.

### File safety

Refused server-side, never on the `accept` attribute alone: anything but
`.csv`, files over 2 MB, empty files, header-only files, missing columns,
duplicate columns, and anything that is not UTF-8. Macro-enabled workbooks are
refused by extension — a macro workbook is a program, and this accepts data.

The uploaded file is never stored; its rows are, as parsed text. The filename
is kept for the audit trail with separators, control characters and
bidirectional overrides stripped — a right-to-left override can make
`evil.csv.exe` render as `exe.csv.live`.

### Permissions

| Role | Import master data | Import opening draft | View history |
|---|---|---|---|
| OWNER | ✓ | ✓ | ✓ |
| MANAGER | ✓ | | ✓ |
| ACCOUNTING_MANAGER | | ✓ | ✓ |
| ACCOUNTANT | | | ✓ |
| STOREKEEPER / PURCHASING / VIEWER / CASHIER | | | |

Three permissions rather than one, because they are three different risks.
Reading history is separate from both applies: an accountant who may apply
nothing still has to be able to see what was applied and by whom.

Authorization is permission **plus** membership in the batch's own
organization. A global Django permission reaches nothing on its own.

## Projection verification

    manage.py verify_stock_projection --organization DEMO-KHAN-MANDI
    manage.py verify_stock_projection --organization KM --warehouse MAIN --item RICE-272

Replays immutable movements in `posted_sequence` order into a shadow
projection, then compares it with `StockBalance` on quantity, value, average
cost, control account, last movement and last posted sequence — in both
directions, so a ledger position with no balance row is reported too.

Exit 0 clean, **1 on drift**, **2 on a selector that names nothing** — silence
must never read as "verified".

### There is no repair mode, deliberately

A safe repair needs all of: an organization maintenance lock, a guarantee that
nothing is posting concurrently, an explicit flag, a stated reason, an
identified actor, a backup warning, one transaction, audit evidence, and a
final verification before commit. Any one missing turns "repair" into
overwriting the evidence of a defect with a plausible number — worse than the
drift, because afterwards nobody can tell what happened.

Balances are a projection of immutable movements and cannot legitimately differ
from them. A difference means something wrote a balance that no posting
produced. Read it; do not erase it.

## Demo data

`seed_inventory_demo` covers every screen above. See
`docs/development/demo-data-policy.md`. Task 1.7A added: reorder points placing
one item below, one exactly at and one above its point; three dated chicken
lots covering expired, expiring-soon and later; one APPLIED import batch that
set those reorder points through the real import service; and one
FAILED_VALIDATION batch holding a valid row that was correctly never applied.

## Procurement's kinds (Task 2.17)

The framework is one and the kinds name which module's master data a batch
carries. `apps/procurement/imports.py` registers `SUPPLIER`,
`SUPPLIER_ITEM` and `PURCHASE_REQUEST_DRAFT` — validators, writers,
required columns, compound row identities (`EXTERNAL_KEYS`: a catalogue
row is supplier *and* item *and* start date, never item alone) and per-kind
permissions (`KIND_PERMISSIONS`: `procurement.import_supplier`,
`procurement.import_supplier_item`, and the draft kind rides
`procurement.create_purchase_request`) — from its `AppConfig.ready`.
Inventory never imports procurement.

Every writer calls the real procurement service, so an import can never do
what a person could not. The draft kind groups rows sharing (warehouse,
required date, purpose) into **one** purchase-request draft that draws no
number and submits nothing; there is no kind for any posted document
(§16.8), and a test asserts the vocabulary stays that way. The upload,
preview, all-or-nothing apply, content-hash retry guard and file security
above apply to these kinds unchanged.

Tests: `apps/procurement/tests/test_procurement_imports.py`.

### Cleaned values are what the service will store

A validator normalises each cell to exactly what the write service would
persist — `strip()` on text, the shared Iraqi-mobile canonicaliser on a
supplier phone. Two consequences, both deliberate:

- The **preview shows the value that will land**, not the one that was
  typed. `07701234567` previews as `+9647701234567` because that is what
  the supplier master will hold.
- A row restating what a record already holds compares **equal** and counts
  as `unchanged`. Comparing raw text against stored text would report a
  change on every re-import and make `applied_row_count` meaningless — the
  defect this rule was written to close.

A value the service would refuse (an unusable phone) is therefore a **row
error in the preview**, with a row number attached, rather than an
exception thrown halfway through an otherwise atomic apply.
