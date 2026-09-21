"""
Minimum fuel octane, and maximum ethanol, as value types.

One octane field, not two. "US grade" and "European grade" are two regional
expressions of one physical fact -- the fuel this engine needs -- so the region
lives in the value, not the field name. The value is a MINIMUM: a manual says
"91 or higher", a rider running 93 is not contradicting it, that is an alternate.

WHY THERE IS NO CONVERTER
    AKI (US pump) = (RON + MON) / 2
    RON (EU pump) = one of the two inputs to that average
AKI cannot be computed from RON, because MON is the other input and no owner's
manual publishes it. The RON-to-AKI gap runs roughly 4-6 points depending on the
blend. So this is an EQUIVALENCE TABLE of published conventions, and every
equivalence carries how it was arrived at, so the page can show a sourced
pairing differently from an estimate.

Stored as the grade the manual named, in the system the manual used --
"AKI:91", "RON:95" -- never the converted figure. That is display, computed on
read, and it is what keeps the badge honest: the stated side is what the manual
said, the other side is an equivalence and renders as one.

Ethanol is its own field with the opposite shape. Octane is a minimum the
engine needs; ethanol is a maximum the fuel system tolerates. A 1973 CB125S
with its original lines is "E0 only" whatever its octane, and fusing the two
would stop an alternate saying "I run 93 but still E0".

Grades, equivalences and their provenance come from fuel_octane.sql (Sep 2026).
"""

# ---- pump grades: the closed set --------------------------------------------
# is_common=False marks real but regional grades, sorted below the everyday
# ones so the picker is not cluttered with high-altitude and E15 grades.
GRADES = [
    # system, value, label,                    region_note,                                   common
    ("AKI",  85, "85 (AKI)",                   "High-altitude US states only (CO, UT, WY, MT)", False),
    ("AKI",  86, "86 (AKI)",                   "High-altitude regular; also older US manuals",  False),
    ("AKI",  87, "87 Regular (AKI)",           "Standard US regular",                           True),
    ("AKI",  88, "88 (AKI)",                   'E15 / "Unleaded 88" at some US stations',       False),
    ("AKI",  89, "89 Midgrade (AKI)",          "Standard US midgrade",                          True),
    ("AKI",  91, "91 Premium (AKI)",           "US premium, common on the west coast",          True),
    ("AKI",  92, "92 (AKI)",                   "US premium in some states",                     False),
    ("AKI",  93, "93 Premium (AKI)",           "US premium, common east of the Rockies",        True),
    ("AKI",  94, "94 (AKI)",                   "Rare US/Canada high-grade",                     False),
    ("RON",  91, "91 RON",                     "Low-grade / older European spec",               False),
    ("RON",  95, "95 RON (Euro 95 / E5-E10)",  "Standard European petrol",                      True),
    ("RON",  98, "98 RON (Super Plus)",        "European premium",                              True),
    ("RON", 100, "100 RON",                    "European high-grade",                           False),
]

# ---- cross-region equivalence: stored one way, read both ways --------------
# (ron, aki_min, aki_max, method, source). A range where the published
# convention is a range -- 95 RON is quoted as 90-91 AKI, and collapsing that
# to one figure would be inventing precision.
EQUIVALENTS = [
    ( 91, 87, 87, "published", "fuel-prices.eu fuel-grades table"),
    ( 95, 90, 91, "published", "fuel-prices.eu fuel-grades table"),
    ( 98, 93, 93, "published", "fuel-prices.eu fuel-grades table"),
    (100, 95, 95, "published", "fuel-prices.eu fuel-grades table"),
    # formula fill-ins where nothing is published: AKI ~ RON - 5. Honest
    # arithmetic, but an estimate, and shown as one.
    ( 90, 85, 85, "formula", "AKI = RON - 5"),
    ( 92, 87, 87, "formula", "AKI = RON - 5"),
    ( 94, 89, 89, "formula", "AKI = RON - 5"),
    ( 96, 91, 91, "formula", "AKI = RON - 5"),
    ( 97, 92, 92, "formula", "AKI = RON - 5"),
    ( 99, 94, 94, "formula", "AKI = RON - 5"),
]

# ---- ethanol: a maximum the fuel system tolerates ---------------------------
ETHANOL = [
    ("E0",  "E0 — ethanol-free only",
     "Original rubber lines and carb parts on older bikes; ethanol swells and cracks them"),
    ("E5",  "E5 — up to 5% ethanol",   "Common European pump grade"),
    ("E10", "E10 — up to 10% ethanol", "Standard US and European pump fuel"),
    ("E15", "E15 — up to 15% ethanol", '"Unleaded 88" at some US stations; most manuals exclude it'),
]

_GRADE = {(s, v): (lbl, note, common) for s, v, lbl, note, common in GRADES}
_BY_RON = {ron: (lo, hi, method, src) for ron, lo, hi, method, src in EQUIVALENTS}
_ETHANOL = {k: (lbl, note) for k, lbl, note in ETHANOL}


class FuelValueError(ValueError):
    """A value that is not a grade on the list."""


# ---- octane -----------------------------------------------------------------
def parse_octane(value):
    """('AKI', 91) for 'AKI:91', 'aki 91', '91 AKI', or a bare '87'.

    A bare number is accepted only where it is unambiguous: 87 is a US grade
    and not a European one, so '87' typed before this existed means AKI:87.
    """
    if value is None or not str(value).strip():
        raise FuelValueError("an octane grade is required")
    raw = str(value).strip().upper().replace("-", " ").replace(":", " ")
    parts = raw.split()
    system, number = None, None
    for p in parts:
        if p in ("AKI", "RON"):
            system = p
        elif p.isdigit():
            number = int(p)
    if number is None:
        raise FuelValueError(f"{value!r} has no octane number in it")
    if system is None:
        hits = [s for (s, v) in _GRADE if v == number]
        if len(hits) == 1:
            system = hits[0]
        elif not hits:
            raise FuelValueError(f"{number} is not a pump grade on the list")
        else:
            raise FuelValueError(f"{number} exists in both AKI and RON — say which")
    if (system, number) not in _GRADE:
        raise FuelValueError(
            f"{number} {system} is not a pump grade on the list "
            f"({', '.join(str(v) for s, v in _GRADE if s == system)} {system})")
    return system, number


def normalise_octane(value):
    system, number = parse_octane(value)
    return f"{system}:{number}"


def equivalent(system, number):
    """The other region's grade, as (system, lo, hi, method) or None.

    RON -> AKI reads the table directly. AKI -> RON is the reverse lookup, and
    genuinely ambiguous -- 91 AKI sits inside both the 95 RON and 96 RON rows --
    so it comes back as a range. Where the range draws on a mix of published
    and formula rows the whole thing is labelled formula: the conservative
    direction, on purpose.
    """
    if system == "RON":
        row = _BY_RON.get(number)
        if not row:
            return None
        lo, hi, method, _ = row
        return ("AKI", lo, hi, method)
    rons = [(ron, method) for ron, (lo, hi, method, _) in _BY_RON.items()
            if lo <= number <= hi]
    if not rons:
        return None
    method = "published" if all(m == "published" for _, m in rons) else "formula"
    return ("RON", min(r for r, _ in rons), max(r for r, _ in rons), method)


def describe_octane(value):
    """Everything the page needs to print one octane value."""
    system, number = parse_octane(value)
    label, note, common = _GRADE[(system, number)]
    eq = equivalent(system, number)
    return {
        "system": system, "value": number, "label": label,
        "region_note": note, "is_common": common,
        "equivalent": None if not eq else {
            "system": eq[0], "min": eq[1], "max": eq[2], "method": eq[3],
            "text": (f"{eq[1]} {eq[0]}" if eq[1] == eq[2]
                     else f"{eq[1]}–{eq[2]} {eq[0]}"),
        },
    }


# ---- ethanol ----------------------------------------------------------------
def normalise_ethanol(value):
    if value is None or not str(value).strip():
        raise FuelValueError("an ethanol limit is required")
    key = str(value).strip().upper().replace(" ", "")
    if key in ("0", "E0", "NONE", "ETHANOLFREE", "ETHANOL-FREE"):
        key = "E0"
    elif key.isdigit():
        key = "E" + key
    if key not in _ETHANOL:
        raise FuelValueError(
            f"{value!r} is not an ethanol limit on the list "
            f"({', '.join(k for k, _, _ in ETHANOL)})")
    return key


def describe_ethanol(value):
    key = normalise_ethanol(value)
    label, note = _ETHANOL[key]
    return {"key": key, "label": label, "note": note}


# ---- what a client needs --------------------------------------------------
def vocabulary():
    return {
        "grades": [
            {"system": s, "value": v, "key": f"{s}:{v}", "label": lbl,
             "region_note": note, "is_common": common,
             "equivalent": describe_octane(f"{s}:{v}")["equivalent"]}
            for s, v, lbl, note, common in GRADES
        ],
        "ethanol": [{"key": k, "label": lbl, "note": note} for k, lbl, note in ETHANOL],
        "systems": [
            {"key": "AKI", "label": "US · AKI", "note": "(R+M)/2 — the number on a US pump"},
            {"key": "RON", "label": "Europe · RON", "note": "The number on a European pump"},
        ],
    }
