# Golden case — unit conversion

Hand-calculated cases using real Khan Mandi quantities. Every line here is
asserted by `apps/units/tests/test_conversion.py::TestGoldenCases`. If the code
and this document ever disagree, this document is the specification.

Policy: ADR-006. Quantities store at **3 decimal places**, `ROUND_HALF_UP`
(ties away from zero). Conversion carries full precision and is rounded
**once**, at the storage boundary.

## Base units

| Dimension | Base unit | Code |
|---|---|---|
| Mass | Kilogram | `KG` |
| Volume | Litre | `L` |
| Count | Piece | `PIECE` |

## Factors

| Unit | Dimension | Base units in one | Exact? |
|---|---|---|---|
| `KG` | Mass | 1 | yes |
| `G` | Mass | 0.001 | yes |
| `MG` | Mass | 0.000001 | yes |
| `TON` | Mass | 1000 | yes |
| `L` | Volume | 1 | yes |
| `ML` | Volume | 0.001 | yes |
| `PIECE` | Count | 1 | yes |
| `DOZEN` | Count | 12 | yes |

## Worked cases

### 1. A sack of rice, in grams

Khan Mandi buys rice in 30 kg sacks and consumes it in recipes by the gram.

```
30 KG -> G
  to base:   30 × 1        = 30        kg
  from base: 30 ÷ 0.001    = 30000
  store:     30000.000 G
```

### 2. Recipe spice quantity, grams to kilograms

A portion carries 7 g of spice mix; stock is held in kilograms.

```
7 G -> KG
  to base:   7 × 0.001     = 0.007     kg
  from base: 0.007 ÷ 1     = 0.007
  store:     0.007 KG
```

### 3. Cooking oil, millilitres to litres

```
250 ML -> L
  to base:   250 × 0.001   = 0.25      L
  from base: 0.25 ÷ 1      = 0.25
  store:     0.250 L
```

### 4. Chicken, dozens to pieces

```
3.5 DOZEN -> PIECE
  to base:   3.5 × 12      = 42        pieces
  from base: 42 ÷ 1        = 42
  store:     42.000 PIECE
```

A dozen is always twelve, which is why it is a genuine unit conversion. A
*carton* is not — one carton of cups and one carton of chicken hold different
quantities — so carton is item-specific packaging and belongs to Phase 1.

### 5. Half a chicken

The charter calls for half portions. COUNT units permit fractions at 3 dp.

```
0.5 PIECE -> PIECE
  identity (same unit, no round trip)
  store:     0.500 PIECE
```

### 6. Precision loss — a real operational limit

```
1 MG -> KG
  to base:   1 × 0.000001  = 0.000001  kg
  from base: 0.000001 ÷ 1  = 0.000001
  store:     0.000 KG          <-- rounds to zero
```

**0.000001 is below the 0.0005 tie, so it rounds down to zero.** Anything
measured in milligrams must be held in a unit whose third decimal place is
meaningful. Recording saffron in kilograms would silently record nothing.

## Rounding direction

| Input | Stored (3 dp, HALF_UP) | Note |
|---|---|---|
| `1.0004` | `1.000` | below the tie |
| `1.0005` | `1.001` | tie, away from zero |
| `-1.0005` | `-1.001` | **away from zero, not toward +∞** |
| `1.00049999` | `1.000` | below the tie — see below |

The last row is the double-rounding trap. Quantizing to 6 dp first gives
`1.000500`, which then rounds *up* to `1.001`. Rounding once, directly, gives
`1.000`, which is correct. This is why `convert()` does not round and
`convert_to_stored_quantity()` does.

## Not covered here

- Item packaging conversions (1 sack of *this* rice = 30 kg) — Phase 1.
- Production yield (10 kg raw meat → 8.7 kg trimmed) — Phase 3, and not a
  conversion at all.
- Monetary rounding — undecided, see ADR-006 §Open.
