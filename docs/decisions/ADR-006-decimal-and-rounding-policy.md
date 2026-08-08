# ADR-006 — Decimal and rounding policy (quantities)

- **Status:** Accepted. Covers **quantities**; the monetary policy is ADR-012.
- **Date:** 2026-08-08
- **Related:** ADR-004 (append-only ledgers), `docs/testing/golden-cases/units-conversion.md`

## Context

Every quantity in this system is eventually multiplied by a cost. A quantity
that is wrong in the third decimal place is money that is wrong, and because
posted records are immutable, a rounding mistake is corrected by reversal
rather than by editing — so it stays visible forever. The policy has to be
written down once and applied in exactly one place.

## Decision

As stated by the product owner:

> Use 3 decimal places for normal inventory quantities and unit conversions,
> with ROUND_HALF_UP as the default rounding mode. For recipe calculations and
> intermediate conversions, keep 6 decimal places internally, then round to 3
> decimal places for stored/displayed inventory quantities. Monetary values
> should use their own separate precision and must not use the quantity
> rounding rules.

Implemented in `apps/core/quantity.py`:

| Constant | Value | Meaning |
|---|---|---|
| `QUANTITY_PLACES` | 3 | Stored and displayed inventory quantities |
| `CALCULATION_PLACES` | 6 | Persisted intermediate values (recipe lines) |
| `FACTOR_PLACES` | 12 | Unit conversion factors — **inferred, see Open** |
| `QUANTITY_ROUNDING` | `ROUND_HALF_UP` | Ties away from zero |

### "Round once" — the clarification that changes the numbers

"Keep 6 decimal places internally, then round to 3" is implemented as *carry
full precision, quantize once at the storage boundary*, **not** as
`quantize(6)` followed by `quantize(3)`. Read literally, the second reading
produces different answers:

```
Decimal("1.00049999")
  .quantize(6dp) -> 1.000500   then .quantize(3dp) -> 1.001    WRONG
  .quantize(3dp) directly                          -> 1.000    CORRECT
```

`1.00049999` is below the `1.0005` tie and must round down. The two-step path
manufactures a tie that was not there. This is exactly what CLAUDE.md's
"never round mid-calculation — round once, at the boundary" forbids, so
`CALCULATION_PLACES` is a floor on how precisely an intermediate is *stored*,
never an instruction to round during a computation.

Consequently `convert()` returns full precision and does not round;
`convert_to_stored_quantity()` is the single boundary that does.

### ROUND_HALF_UP means away from zero

Verified, not assumed:

```
Decimal("-1.0005").quantize(Decimal("0.001"), ROUND_HALF_UP) == Decimal("-1.001")
```

This matters because corrections happen by reversal. Rounding must be
symmetric in magnitude, or a reversal would not cancel its original exactly.
"Toward positive infinity" would break that; it is not what this mode does.
Note also that Python's *default* is `ROUND_HALF_EVEN`, which would give
`1.000` for `1.0005` — every quantize call names the mode explicitly.

### PostgreSQL agrees

`SELECT 0.0005::numeric(18,3)` returns `0.001` on PostgreSQL 18.4, and
`-0.0005` returns `-0.001`. The database rounds half away from zero, matching
Python. Values are still quantized in Python before storage so the database
never has to round — the agreement is a safety net, not the mechanism.

### Arabic numerals

`Decimal("١٢٣")` is `123` in CPython, and so is `Decimal("1٢3")`. The first is
useful — operators here type Arabic-Indic numerals. The second is not: a
mixed-script number is almost always damage, such as a mis-segmented OCR read
of a supplier invoice, and Phase 8 plans exactly that ingestion path. So the
conversion is done explicitly and mixing scripts is refused, rather than
inheriting whatever CPython happens to accept.

### Floats

Rejected outright in any quantity path, not coerced. `Decimal(0.1)` is
`0.1000000000000000055511151231257827021181583404541015625`; a float arriving
in a quantity has already lost the exactness the policy exists to preserve.
`bool` is rejected too, being a subclass of `int`.

## Alternatives considered

- **`ROUND_HALF_EVEN`** (banker's rounding) — statistically unbiased over many
  roundings and the usual choice for money. Rejected here because the owner
  specified HALF_UP and because operators verifying a figure by hand expect
  `0.0005` to become `0.001`.
- **Quantizing intermediates to 6dp** — the literal reading. Rejected; see the
  counterexample above.
- **Storing the reciprocal factor** (base units per unit) — would make the
  common direction a division. Division introduces repeating decimals; `1/3`
  has no exact decimal form. Multiplication to base is exact.
- **Letting PostgreSQL round on insert** — works, but moves the policy out of
  the code and out of the tests.

## Consequences

- A quantity below `0.0005` of its stored unit rounds to zero. One milligram
  expressed in kilograms stores as `0.000`. Anything measured in very small
  amounts — saffron, food colouring — must be held in a unit whose 3dp is
  meaningful. This is a real operational constraint, not a bug.
- `apps/core/quantity.py` names every public function "quantity" so a monetary
  amount cannot reuse it by accident. Money gets its own module.
- Changing a conversion factor after stock exists would restate every quantity
  ever converted through it. Factors must be treated as immutable once
  transactions begin; no guard enforces this yet.

## Open — needs a decision

1. ~~Monetary precision and rounding.~~ **Decided** — see
   [ADR-012](ADR-012-monetary-precision-and-allocation.md).
2. ~~Conversion factor precision.~~ **Confirmed at 12.** Not to be reduced.
   Twelve is the smallest value that stores one ounce exactly
   (`0.028349523125` kg). Factors are stored once, from each unit to its
   dimension's base; inverse conversions are derived mathematically rather
   than stored as independent reciprocals, so a unit pair can never carry two
   factors that disagree.
3. **Per-item fraction rules and minimum issue increments.** The charter lists
   these as conversion attributes. They are properties of an *item*, not of a
   unit — half a chicken is meaningful, half a cup is not, and both are COUNT
   — so they are deferred to Phase 1 with the Item model.
