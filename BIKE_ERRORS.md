# Mistakes in the catalogue

Audited 2026-09-28 across all 2,436 bikes with `audit_bikes.py` (read-only).
Re-run it any time:

```
py audit_bikes.py --csv BIKE_ERRORS.csv
```

---

## The root cause, which matters more than any single bike

`answer_questionnaire.py:849` decided whether an engine was a two-stroke:

```python
stroke = 2 if st.startswith("2") else 4 if st.startswith("4") else ... else 4
```

It read a **Stroke** column from the import workbook, and **a blank or
unrecognised cell silently became FOUR**. The importer's own docstring says
Stroke *"has no column in the schema and is left in the workbook"* — so the
value was used to answer the questionnaire and then thrown away, and the
workbook is no longer in the repo.

Two consequences:

1. **A mis-marked two-stroke is invisible from inside the database.** Its
   answers and its specs were both derived from the same wrong premise, so
   they agree with each other perfectly. The audit found **0** internal
   contradictions, and that proves nothing.
2. **Only bikes whose model name betrays the error can be found.** A
   two-stroke whose name gives no clue is undetectable without a person
   who knows the machine.

**588 bikes are marked four-stroke and were made up to 2010** — that is the
pool a mis-marked two-stroke is hiding in.

---

## To correct

### 1. Suzuki JR80 — bike 2276 — WRONG

| | |
|---|---|
| Record says | q5=C, four-stroke with valve clearance adjustment |
| Actually | **two-stroke, 79cc, air-cooled single** |
| Lubrication | **auto-lube** — Suzuki CCIS, "makes manual mixing of fuel and oil unnecessary" |
| Should answer | **q5 = A**, two-stroke auto-lube |

Consequences on the sheet today: it is offered **Intake and Exhaust Valve
Clearance**, which it has not got, and is **not** offered Auto-lube Oil Type
or Auto-lube Oil Tank Capacity, which it needs.

**Years are also wrong.** The record says 2018–2023 with `years_verified=0`.
That came from the missing workbook and no source can be produced for it.
autoevolution lists the JR80 as 2002–2003; Suzuki New Zealand still publishes
a JR80 page, which is how a model runs later in some markets.

### 2. Suzuki RV125 VanVan — bike 2244 — WRONG

Recorded as one bike spanning **1972–2019**, a 47-year run. The model code
itself says `RV125 VanVan (1972-82)`. The VanVan was built 1972–82, dropped,
and revived in 2003. That is **two production runs merged into one bike**, and
they do not share a frame, an engine or a part number.

Should be **split at the gap**, which the site already supports.

### 3. Suzuki RV200 VanVan — bike 2246 — WRONG

Same fault, same span, same fix.

---

## Checked and correct — do not "fix" these

- **GasGas EC 400** (bike 1845, 2002–2006) and **EC 450** (bike 1847,
  2007–2009). The name pattern flags them as EC two-strokes, but these are
  the four-stroke FSE enduros. Correctly marked.
- **Nine bikes with no questionnaire answers at all** — every one is
  **electric**: GasGas MC-E and TXE, Husqvarna EE, KTM Freeride E-XC and
  SX-E, Kawasaki Ninja 7 Hybrid, Piaggio 1, Vespa Elettrica, Victory Empulse
  TT. The importer returns no stroke for an electric, so it asked nothing.
  That is right, not a bug — though it does mean they carry only the universal
  fields, and **the tree has no electric specs at all**: no battery capacity,
  motor rating, charge time or connector type.

---

## What the audit cannot tell you

- **Years.** 2,426 of 2,436 bikes have `years_verified = 0`. The importer was
  honest that a manager still has to confirm each span; almost none have been.
  Only spans that contradict themselves are findable.
- **Stroke, beyond the name.** See the root cause above.
- **Whether a value is right.** Displacement, part numbers and capacities came
  from the same workbook and cannot be checked against anything on the site.

## What would actually fix the class of problem

Re-import the Stroke column into a real field, so it can be audited and
corrected rather than being an invisible premise. Failing that, a rider
answering the questionnaire on their own bike overrides it — which is the
mechanism the site already has, and the reason a wrong answer is recoverable.
