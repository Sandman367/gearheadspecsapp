# Specs the tree is missing

Researched 2026-09-25 against the 148 fields then on the Spec Tree. Every
entry below was checked against the existing list; anything that looked like
it might already be covered is called out rather than quietly proposed twice.

The ordering is by **what a rider cannot find today**, not by what is easy.

---

## Tier 1 — Torque specs

**The single biggest hole. The tree has 148 fields and not one torque value.**

This is the most-searched maintenance number there is: a rider with the wheel
off and a torque wrench in hand needs a figure, and right now the site has
nothing to give them. It is also the one place where a wrong number does
visible damage — a stripped drain plug, a warped rotor, a wheel that comes
loose — which is an argument for sourcing them carefully, not for leaving
them out.

Suggest as `text`, so the value can carry both units the way manuals do
("31 N·m (23 lb·ft)"), with an example set for the shape.

| Field | Gate |
|---|---|
| Oil Drain Plug Torque | every bike |
| Oil Filter Torque | bikes with a spin-on filter |
| Spark Plug Torque | every bike |
| Front Axle Nut Torque | every bike |
| Rear Axle Nut Torque | every bike |
| Brake Caliper Mounting Bolt Torque | disc brakes |
| Brake Disc Bolt Torque | disc brakes |
| Rear Sprocket Nut Torque | chain drive |
| Cylinder Head Bolt Torque | every bike |
| Fork Pinch Bolt Torque | every bike |
| Swingarm Pivot Torque | every bike |
| Rear Shock Mounting Bolt Torque | every bike |
| Handlebar Clamp Bolt Torque | every bike |
| Clutch Spring Bolt Torque | every bike |

Head bolts are torqued in a sequence and in stages, which one text field
cannot really hold. Either accept a compressed value, or pair it with a
**Cylinder Head Torque Sequence** field.

---

## Tier 2 — Service intervals

**Five of the six service tasks in My Garage have no interval spec.** They
fall back to a hardcoded default, so "what's due" is currently a guess for
everything except the oil.

From `service_tasks`: `oil` reads `oil_change_interval`. `plugs`, `valves`,
`chain`, `coolant` and `brakefl` read nothing and default to 16000 / 16000 /
600 / 24000 / 24000 miles.

| Field | Feeds |
|---|---|
| Valve Check Interval | the `valves` task |
| Spark Plug Interval | the `plugs` task |
| Chain Service Interval | the `chain` task |
| Coolant Change Interval | the `coolant` task |
| Brake Fluid Change Interval | the `brakefl` task |
| Air Filter Interval | a task that does not exist yet |
| Fork Oil Change Interval | a task that does not exist yet |

Cheapest high-value work on this list: seven fields wired to a feature that
is already built and currently guessing. Brake fluid and coolant are
**time**-based rather than mileage-based on most bikes (two years regardless
of miles), which the interval machinery may not express — worth checking
before building.

---

## Tier 3 — Wear limits

The "is this still good, or do I replace it?" numbers. The tree records what
a part *is* but never when it is **worn out**, so a rider can look up their
pad part number and still not know whether the pads in their hand need
replacing.

- Front / Rear Brake Pad Minimum Thickness
- Front / Rear Brake Rotor Minimum Thickness *(stamped on the rotor; frequently searched)*
- Brake Rotor Runout Limit
- Brake Drum Maximum Diameter *(drum bikes — you have 462 front and 849 rear shoe rows)*
- Chain Wear Limit *(length over 20 links)*
- Tire Minimum Tread Depth
- Clutch Friction Plate Minimum Thickness
- Clutch Spring Free Length

---

## Tier 4 — Suspension

**Your weakest category: 6 fields.** Suspension is one of the things riders
most want numbers for and least often find.

- Fork Oil Capacity (per leg)
- Fork Tube Diameter
- Fork Spring Rate
- Front Suspension Travel
- Rear Suspension Travel
- Rear Shock Spring Rate
- Recommended Rider Sag
- Front Fork Compression / Rebound Settings
- Rear Shock Compression / Rebound Settings

⚠️ **Check first:** you already have *Front Suspension Fluids* and *Front
Shock Oil Level*. Fork oil **weight** may already be covered by the first,
and fork oil **level** by the second. Capacity per leg and level are
genuinely different numbers — manuals give both — but confirm before adding.

---

## Tier 5 — Charging and ignition

**No charging-system coverage at all**, and a dead battery that is really a
dead stator is one of the commonest faults on an older bike.

- Stator Output *(watts, or VAC at a stated RPM)*
- Charging Voltage at RPM *(the 13.5–14.8 V check)*
- Regulator / Rectifier Part Number
- Ignition Coil Resistance (primary / secondary)
- Pickup / Pulse Coil Resistance
- Main Fuse Amp Rating *(you have Fuse Type and Fuse Box Location, but no main rating)*
- Horn Wire Color *(you track wire colours for starter, kill switch, turn signals, kickstand)*

⚠️ *Battery* exists but may be a part number only. Battery **type/size**
(YTX12-BS), **CCA** and **Ah** may want splitting out.

---

## Tier 6 — Carburettor detail

You have main and pilot jets for up to six carbs, and nothing else. A rebuild
needs more than jets.

- Float Height
- Pilot / Air Screw Turns Out
- Jet Needle Part Number
- Needle Clip Position
- Needle Jet
- Slide Cutaway
- Throttle Cable Freeplay
- Fuel Petcock Type

Gate all of these on the existing carb branch, so injected bikes never see them.

---

## Tier 7 — Wheels and tyres

- Front / Rear Rim Size *(you have tyre size but not rim width or diameter)*
- Front / Rear Wheel Bearing Size
- Front / Rear Axle Diameter
- Spoke Torque *(spoked wheels)*
- Tyre Pressure, Two-Up / Loaded *(you have one pressure per end; manuals give two)*

---

## Tier 8 — Engine internals

For rebuilds rather than maintenance. Lower priority for most riders, but
this is where the vintage crowd lives, and your catalogue runs back to 1901.

- Cylinder Compression *(spec PSI — the number you test against)*
- Piston Ring End Gap
- Piston-to-Cylinder Clearance
- Oil Pressure
- Cam Chain Tensioner Part Number
- Cam Timing Marks

---

## Tier 9 — Identification

Cheap, universal, and genuinely hard to find.

- VIN Location
- Engine Number Location
- **Paint Code** — *not the same as Factory Color*. "Candy Glory Red" is the
  name; "R-157C" is what the paint shop needs.
- Key Blank Number

---

## What this would cost

Roughly **80 fields**. Do not add them all at once.

Every universal field creates a row on all 2,436 bikes. Eighty universal
fields would be ~195,000 new empty rows, and every bike's "specs filled"
percentage would fall off a cliff — honest, but demoralising, and it buries
the gaps that matter under the gaps that do not.

**Suggested order:**

1. **Tier 2 (intervals)** — 7 fields, wired to a feature already built and
   currently guessing. Highest value per field on the list.
2. **Tier 1 (torque)** — 14 fields. The biggest gap, and the thing riders
   most often arrive looking for.
3. **Tier 3 (wear limits)** — 8 fields, and they pair naturally with the
   Brakes category just created.
4. Everything else as demand shows up — the **bike requests** queue and the
   **field proposals** queue will tell you what riders actually want, which
   beats guessing from a list.

Gate everything possible on the questionnaire. A carb field on an injected
bike, or a chain field on a belt drive, is the fault this project has spent
its time removing.
