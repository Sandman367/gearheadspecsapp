# What riders are actually looking for

Researched 2026-09-26 across public motorcycle forums. **Facebook groups are
behind a login and could not be read**; Reddit blocks the crawler. What
follows comes from forums that are publicly indexed — Harley-Davidson Forums,
HDForums, Britbike, VTXOA, ThumperTalk, KZRider, DirtRider, Vital MX,
Kawiforums, TriumphRat, GL1800Riders, Motorcycle Forum, AMCA — plus the
technical references those threads keep linking to.

Sorted by **how often the same question gets asked**, not by how easy it is
to answer.

---

# Part A — Things people want that are not spec fields

These came out of the research unprompted and matter more than any single
field, because no site is serving them well.

## A1. "Will this part fit my bike?"

**The strongest finding in the whole search.** Parts-interchange threads are
everywhere — Kawasaki, Triumph, Harley, Honda, Goldwing — and forum members
build interchange lists by hand, thread by thread. One thread states the
problem outright: there is no dedicated site where you enter a part number
and find what else uses it.

**GearHeadSpecs is already sitting on the data.** The site stores part
numbers per bike per spec. "Which other bikes list this same oil filter"
is a `GROUP BY value` over a column that already exists. No new field, no
new research — a query and a page.

The usual workaround people describe is comparing part numbers across model
years in OEM parts diagrams, one at a time, by eye.

**Honest caveat on the data as it stands.** 698 value-groups are already
shared by more than one bike, but most are catalogue-seeded engine
displacement and cylinder configuration rather than part numbers. Only 86
values on the site were typed by a person. So interchange would work, and
would be right, but it would look thin until riders fill the sheets in. That
is an argument for building it early rather than late: it gets better on its
own, and it gives a rider a reason to enter a part number, because entering
one makes their bike findable from every other bike that shares the part.

## A2. Wiring colours, in colour

A VTX thread notes that colour wiring diagrams were not in the official
manual and community members had to make them. You already track wire
colours as a first-class value type, with per-manufacturer vocabulary —
starter switch, kill switch, turn signals, kickstand. **This is a genuine
strength nobody else has**, and it is worth extending rather than treating
as a curiosity:

- Ignition switch wire colours
- Charging / stator wire colours
- Horn wire colour
- Neutral switch wire colour
- Oil pressure switch wire colour
- Fuel pump wire colour
- Coil primary / secondary wire colours

## A3. Dirt bikes run on hours, not miles

`service_log` records `miles`, and `service_tasks` has only
`default_interval_miles`. **471 of your bikes are dirt bikes**, and that
world measures everything in engine hours — top-end intervals, air filter,
oil. A rider with an hour meter cannot use My Garage as it stands.

## A4. Jetting is meaningless without altitude

Forum jetting advice is consistent: jetting must be set for the elevation
and conditions you actually ride in. A single "Main Jet" value with no
context is half a spec. Either note the altitude a value was set at, or
accept that jetting is a **community** spec where several answers are
legitimately right — which is what alternates already exist for.

## A5. Symptom → what to check

The commonest forum thread of all is "it won't start" / "it won't charge" /
"it runs badly since I cleaned the carb". This is not a spec and probably
not your fight, but it is worth knowing it is the traffic every motorcycle
site competes for. Battery issues alone are cited as ~35% of no-start calls.

---

# Part B — Specs people hunt for

## B1. Torque, again

Already the top item in `SPEC_GAPS.md`, and the forum evidence is blunt: a
Britbike thread exists purely because the factory manual **omitted** swingarm
pivot and axle torque. Harley forums carry the same threads for primary cover
bolts, inner primary, rear axle.

Notable: values come in **inch-pounds** as often as foot-pounds on small
fasteners (primary cover quoted at 84–108 in-lb). A single text field holds
this fine; the example should show it.

## B2. Belt and primary drive — you have 538 belt bikes and no belt numbers

| Missing | Notes |
|---|---|
| **Drive Belt Deflection / Tension** | You have *Drive Chain Slack* for chains and nothing for belts. Quoted as "6 mm at 10 lb". The single clearest omission found. |
| **Primary Chain Deflection** | Harley item, quoted as 5/8–7/8 in free play. You have *Primary Oil* and *Primary Oil Volume* but no adjustment figure. |
| **Belt Tension Tool** | Checking it needs a specific tool at room temperature. |

**218 of your Harleys are belt-driven.** This is the group with the most
bikes and the least data.

## B3. Carburettor, beyond jets

Vintage restorers report guessing at numbers they cannot find, including
assuming a points gap when no spec exists. You already have *Points Gap*.
You do not have:

- **Float Height** — repeatedly discussed, tolerance around ±0.5 mm, and the
  single most-cited carb number after jets
- Pilot / Air Screw Turns Out
- Jet Needle part number and clip position
- Needle Jet
- Slide Cutaway
- Fuel Petcock Type — vacuum vs manual, a common no-start cause
- Vacuum Line Routing — cited as a frequent post-cleaning failure

## B4. Fluids and capacities

"What oil?" and "how much?" are perennial. You cover engine, gear,
transmission, primary, drive shaft and fork fluids well. Gaps:

- Brake Fluid Capacity
- Fork Oil Capacity **per leg** (distinct from level, which you may have)
- Coolant type detail — silicate-free / OAT matters on aluminium engines

## B5. Battery, properly

*Battery* exists, probably as a part number. Riders ask by **size and
rating**, and cross-shop by them:

- Battery Size / Group (YTX12-BS)
- Battery CCA
- Battery Amp Hours
- Battery Terminal Orientation — the thing that makes a "compatible" battery
  not fit

## B6. Charging system

No coverage at all, and charging faults are among the most-discussed
problems on every forum. Regulated voltage lands in a 13.5–14.8 V band and
that is exactly the number people test against.

- Stator Output (watts, or VAC at stated RPM)
- Charging Voltage at RPM
- Stator Resistance
- Regulator / Rectifier Part Number
- Regulator Type — shunt vs series vs MOSFET, which decides what interchanges

## B7. Tyres and wheels

Tyre sizing is one of the most-asked beginner topics. You have tyre size and
pressure. Missing:

- Front / Rear Rim Size (width and diameter)
- Load Index / Speed Rating
- Tyre Pressure, Two-Up or Loaded — manuals give two figures, you store one
- Tyre Minimum Tread Depth
- Wheel Bearing Size
- Axle Diameter

## B8. Wear limits

Nothing on the tree says when a part is finished. Covered in `SPEC_GAPS.md`;
the forum evidence reinforces pad and rotor minimum thickness in particular,
since the rotor figure is stamped on the part and people still cannot find
what it should be.

---

# What I would do with this

1. **Part interchange.** No new data, no new fields, and nobody else offers
   it. The biggest return on the smallest build on this list.
2. **Belt and primary drive numbers.** Your largest under-served group, and
   a one-line gap next to a field you already have for chains.
3. **Engine hours in the garage.** 471 dirt bikes currently cannot use the
   service log honestly.
4. **Float height**, then the rest of the carb set.
5. Torque and wear limits as already planned.

## What I could not check

Facebook groups. They are the biggest single venue for exactly this kind of
question, and they are closed to me. If you can export or paste threads from
the groups you are in, the same analysis would run over them — and would
likely be sharper than public forums, since Facebook is where the
model-specific groups now live.
