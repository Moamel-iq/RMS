# Phase 3 — the owner data gate on real recipe quantities

**Status: OPEN.** The machinery to load the Khan Mandi recipe book exists (or is
being built); the recipe book has not been filled in or signed.

This page exists so that "Phase 3 is complete" is never read as "the kitchen's
real recipes are in the system". Those are different claims and only the first
one is going to be true.

## What was audited

The protected sources were inspected **read-only** on 2026-08-19, outside the
repository. Neither was modified, converted, or copied in. Their paths and
SHA-256 digests are recorded in the session scratchpad, deliberately not here —
a hash of a proprietary file, alongside its local absolute path, is itself
something this repository should not carry.

- `KhanMandiRecipe.xlsx` — the `KM-RCP-004` form. 23 sheets: a cover, an item
  register, a packing guide, **19 recipe cards**, and a final approval sheet.
  No macros, no external links.
- `كتاب وصفات المطبخ خان مندي.pdf` — the recipe book.

## What the audit found

Every one of the 19 recipe cards carries the same ingredient table, and the same
pattern of what is and is not filled in:

| Column | Content |
|---|---|
| `H` المكون المقترح — ingredient | **filled** |
| `G` الوحدة — unit | **filled** |
| `F` كمية القياس — measurement quantity | **empty on every row** |
| `E` الكمية المعتمدة — approved quantity | **empty on every row** |
| `D` فاقد % — waste rate | **empty on every row** |
| `C` / `B` unit cost, component cost | **empty on every row** |

And on the approval sheet:

- `تاريخ الاعتماد` — approval date: **blank**
- `الإصدار` — issue/version: **blank**
- `التواقيع` — the four signature blocks (الشيف, المحاسب, أمين المخزن,
  مدير الفرع) each read `الاسم والتوقيع` with **no name and no signature**
- the footer is a literal blank rule: `تاريخ الاعتماد: ____ / ____ / ______`

Per-card fields `يعتمد من تاريخ` (effective from) and `رمز الصنف` (item code) are
also empty.

**The workbook is a blank, unsigned template.** It names the dishes and their
ingredients and units; it states no quantity for any ingredient of any recipe.

## What follows from that

Task 3.10's import framework is generic and is built regardless — its
correctness does not depend on the source being filled. But:

```
REAL KHAN MANDI RECIPE DATA — NOT ACCEPTED
GENERIC IMPORT CODE — COMPLETE
DEMO DATA — FICTIONAL
```

Specifically, and these are prohibitions rather than preferences:

- **No real Khan Mandi recipe quantities are imported**, because none exist.
- **No missing quantity is invented.** A plausible-looking gram figure in a
  costing system becomes a plate cost, becomes a menu price, becomes a variance
  somebody is held to. There is no safe fabrication here.
- **No half / whole / 350 g / 500 g behaviour is inferred.** The card titles
  distinguish حبة كاملة from نصف حبة, but with both quantity columns empty there
  is nothing to compare and no ratio to derive.
- Every imported record in the demo is fictional and `DEMO`-namespaced.

## The decisions this blocks

`KD-02`, `KD-05` and `KD-06` in the Phase 3 decision register stay unresolved,
and for one reason rather than three: each asks a question about approved
quantities or serving policy, and the source does not answer any of them.

## What would close the gate

The owner returns a `KM-RCP-004` workbook in which, for each recipe card:

1. `الكمية المعتمدة` is filled for every ingredient row;
2. `رمز الصنف` resolves to a real `InventoryItem` code;
3. `يعتمد من تاريخ` carries an effective date;
4. the four signature blocks carry names, and the approval date is filled.

At that point the import runs against the real file, the records land as
`DRAFT`, and they become authoritative only by completing the normal
maker-checker lifecycle — never by the import saying so.

## A note on the audit itself

The first parse of the workbook reported it as entirely empty, which was wrong:
the sheets use the `x:` namespace prefix and inline `<x:v>` values rather than a
shared-string table, so a namespace-blind reader matched nothing. The finding
above rests on the corrected parse, and the mistake is recorded because
"the file is empty" and "my reader could not see the file" are conclusions that
look identical from the outside and mean opposite things.
