"""
Fuses as a value type, not as free text.

A fuse is three facts that belong together: what SIZE it is, what it is RATED
at, and what it PROTECTS. Recorded as three text fields -- which is how this
site had them -- they drift apart: a bike ends up with "Starter Fuse Type:
mini" and "Starter Fuse Amp Rating: 10" and no way to tell whether anybody
checked them at the same time, and a rider standing at the fuse box with a
blown fuse in their hand has to read two rows to learn one thing.

Recorded together, the colour comes free. Automotive blade fuses are colour
coded by rating under ISO 8820 / DIN 72581 / SAE J1888, so the page can draw
the fuse the rider is looking for instead of describing it.

THE COLOUR DEPENDS ON THE FAMILY, NOT THE RATING ALONE. A violet regular
blade is 3A; a violet Maxi is 100A. Blue is 15A as a regular blade and also
15A OR 60A as a Maxi. That is why the family is part of the value and not a
separate field -- a rating without its family cannot be drawn, and a colour
guessed from the wrong table is exactly the wrong spec this site exists to
stop. Nothing here derives a rating FROM a colour; it only ever goes the
other way, which is well defined.

Glass and ceramic fuses are not colour coded at all. They are accepted, drawn
as glass, and say their rating in writing -- which is what the part does.
"""

# ---------------------------------------------------------------------------
# What the jackets look like. `clear` is the one that is not a colour: a
# 25A blade is natural translucent plastic, so it is drawn as glass.
# ---------------------------------------------------------------------------
COLORS = {
    "gray":      {"name": "Grey",       "hex": "#8E9096"},
    "violet":    {"name": "Violet",     "hex": "#8E6FD0"},
    "pink":      {"name": "Pink",       "hex": "#F09AB8"},
    "tan":       {"name": "Tan",        "hex": "#C9A66B"},
    "brown":     {"name": "Brown",      "hex": "#6E3F1E"},
    "red":       {"name": "Red",        "hex": "#D42B23"},
    "blue":      {"name": "Blue",       "hex": "#1F5FBF"},
    "yellow":    {"name": "Yellow",     "hex": "#F3D22B"},
    "clear":     {"name": "Clear",      "hex": "#D8D8D2"},
    "green":     {"name": "Green",      "hex": "#1E8A3C"},
    "bluegreen": {"name": "Blue-green", "hex": "#1E8A7C"},
    "orange":    {"name": "Orange",     "hex": "#F07A1C"},
    "purple":    {"name": "Purple",     "hex": "#6A3FA6"},
}

# Amp -> colour, per family. Read off ISO 8820 / DIN 72581; the families that
# share a table share it here rather than repeating it, so a correction lands
# in one place.
_MICRO = {5: "tan", 7.5: "brown", 10: "red", 15: "blue", 20: "yellow",
          25: "clear", 30: "green"}
_BLADE = {2: "gray", 3: "violet", 4: "pink", 5: "tan", 7.5: "brown", 10: "red",
          15: "blue", 20: "yellow", 25: "clear", 30: "green", 35: "bluegreen",
          40: "orange"}
_MAXI = {15: "blue", 20: "yellow", 30: "green", 35: "brown", 40: "orange",
         50: "red", 60: "blue", 70: "tan", 80: "clear", 100: "violet",
         120: "purple"}
# Not colour coded. The ratings are the ones these are commonly sold in; the
# fuse says its rating on the cap, which is what a rider reads.
_GLASS = {r: None for r in (1, 2, 3, 4, 5, 6, 7.5, 10, 15, 20, 25, 30)}

FAMILIES = {
    "micro2":  {"name": "Micro2", "codes": "APT, ATR", "mm": "9.1 x 3.8 x 15.3",
                "shape": "blade1", "amps": _MICRO},
    "micro3":  {"name": "Micro3", "codes": "ATL", "mm": "14.4 x 4.2 x 18.1",
                "shape": "blade3", "amps": _MICRO},
    "lpmini":  {"name": "Low-profile mini", "codes": "APS, ATT", "mm": "10.9 x 3.81 x 8.73",
                "shape": "blade2", "amps": _BLADE},
    "mini":    {"name": "Mini", "codes": "APM, ATM", "mm": "10.9 x 3.6 x 16.3",
                "shape": "blade2", "amps": _BLADE},
    "regular": {"name": "Regular", "codes": "ATO, ATC, APR, ATS", "mm": "19.1 x 5.1 x 18.5",
                "shape": "blade2", "amps": _BLADE},
    "maxi":    {"name": "Maxi", "codes": "APX", "mm": "29.2 x 8.5 x 34.3",
                "shape": "blade2", "amps": _MAXI},
    "glass":   {"name": "Glass tube", "codes": "AGC, AGU", "mm": "",
                "shape": "glass", "amps": _GLASS},
    "ceramic": {"name": "Ceramic (torpedo)", "codes": "GBC", "mm": "",
                "shape": "torpedo", "amps": _GLASS},
}

# What people write, folded onto the family key. The trade codes matter: a
# manual says ATO, a parts bin says ATC, and a rider should be able to type
# whichever one is in front of them.
ALIASES = {}
for _key, _f in FAMILIES.items():
    ALIASES[_key] = _key
    ALIASES[_f["name"].lower().replace(" ", "").replace("-", "")] = _key
    for _code in _f["codes"].split(","):
        _code = _code.strip().lower()
        if _code:
            ALIASES[_code] = _key
ALIASES.update({
    "standard": "regular", "ato": "regular", "atc": "regular", "blade": "regular",
    "lowprofile": "lpmini", "lowprofilemini": "lpmini", "miniloprofile": "lpmini",
    "micro": "micro2", "agc": "glass", "agu": "glass", "glasstube": "glass",
    "torpedo": "ceramic", "bosch": "ceramic", "continental": "ceramic",
})

# A whole fuse box, not a row of one. A wiring diagram prints the box as a
# table -- every fuse, its rating, and what it feeds -- and that table is one
# fact about the bike. Twenty-four covers a big tourer with two boxes; past
# that it is a car.
MAX_FUSES = 24
MAX_ROLE = 40


class FuseError(ValueError):
    """A value that is not a fuse this vocabulary can express."""


def _slug(part):
    return "".join(ch for ch in str(part).lower() if ch.isalnum())


def _amp_text(amp):
    """7.5 stays 7.5; 10.0 becomes 10. The rating is printed on the fuse and
    should read the way it is printed."""
    return f"{amp:g}"


def family_of(word):
    key = ALIASES.get(_slug(word))
    if not key:
        raise FuseError(
            f"{word!r} is not a fuse size this knows. "
            "One of: " + ", ".join(f["name"] for f in FAMILIES.values()))
    return key


def parse(chunk):
    """(family key, amp) for one fuse, in either order and with or without
    the A: "mini 10A", "10A mini", "ATO 15" all mean the same thing."""
    words = [w for w in str(chunk).replace("/", " ").split() if w.strip()]
    if not words:
        raise FuseError("a fuse needs a size and a rating")
    family = amp = None
    for w in words:
        cleaned = w.strip().rstrip(",")
        num = cleaned[:-1] if cleaned.lower().endswith("a") else cleaned
        try:
            value = float(num)
        except ValueError:
            if family is not None:
                raise FuseError(f"{chunk!r} names more than one fuse size")
            family = family_of(cleaned)
            continue
        if amp is not None:
            raise FuseError(f"{chunk!r} has more than one rating in it")
        amp = value
    if family is None:
        raise FuseError(f"{chunk!r} does not say what size fuse it is -- "
                        "mini, regular, maxi, micro2, glass and so on")
    if amp is None:
        raise FuseError(f"{chunk!r} does not say what the fuse is rated at")
    if amp <= 0:
        raise FuseError("a fuse rating has to be above zero")

    table = FAMILIES[family]["amps"]
    if amp not in table:
        known = ", ".join(_amp_text(a) + "A" for a in sorted(table))
        raise FuseError(
            f"{FAMILIES[family]['name']} fuses do not come in {_amp_text(amp)}A. "
            f"The ratings in this size are: {known}")
    return family, amp


def parse_set(value):
    """[(role_or_None, family, amp), ...] for a fuse value.

    Several fuses on one spec, the same as several wires on one: a fuse box
    row is one fact about the bike, not five competing answers.
    """
    if value is None or not str(value).strip():
        raise FuseError("a fuse needs a size and a rating")
    chunks = [c.strip() for c in str(value).split(",") if c.strip()]
    if not chunks:
        raise FuseError("a fuse needs a size and a rating")
    if len(chunks) > MAX_FUSES:
        raise FuseError(f"at most {MAX_FUSES} fuses on one spec")

    out = []
    for chunk in chunks:
        role = None
        if ":" in chunk:
            role, chunk = chunk.split(":", 1)
            role, chunk = role.strip(), chunk.strip()
            if not role:
                role = None
            elif len(role) > MAX_ROLE:
                raise FuseError(f"what a fuse protects is limited to {MAX_ROLE} characters")
        family, amp = parse(chunk)
        out.append((role, family, amp))
    # Two fuses CAN protect the same thing, and a fuse box has repeats. No
    # uniqueness rule here; the ratings and the order tell them apart.
    return out


def normalise_set(value):
    """The form that goes in the database."""
    return ", ".join(
        (f"{role}: " if role else "") + f"{family} {_amp_text(amp)}A"
        for role, family, amp in parse_set(value))


def color_of(family, amp):
    """The jacket colour for this rating IN THIS FAMILY, or None where the
    family is not colour coded. Never the other way round: blue is 15A and
    60A in Maxi, so a colour does not name a rating."""
    return FAMILIES[family]["amps"].get(amp)


def describe_set(value):
    """One readable entry per fuse, for a page or a screen reader."""
    out = []
    for role, family, amp in parse_set(value):
        f = FAMILIES[family]
        key = color_of(family, amp)
        out.append({
            "role": role,
            "family": family,
            "family_name": f["name"],
            "codes": f["codes"],
            "mm": f["mm"],
            "shape": f["shape"],
            "amp": amp,
            "amp_text": _amp_text(amp) + "A",
            "color": key,
            "color_name": COLORS[key]["name"] if key else None,
            "hex": COLORS[key]["hex"] if key else None,
            "value": f"{family} {_amp_text(amp)}A",
        })
    return out


def vocabulary():
    """Everything the picker needs: the families, and the ratings each one
    comes in with the colour that rating is."""
    return {
        "families": [
            {"key": k, "name": f["name"], "codes": f["codes"], "mm": f["mm"],
             "shape": f["shape"],
             "amps": [{"amp": a, "text": _amp_text(a) + "A",
                       "color": f["amps"][a],
                       "hex": COLORS[f["amps"][a]]["hex"] if f["amps"][a] else None,
                       "color_name": COLORS[f["amps"][a]]["name"] if f["amps"][a] else None}
                      for a in sorted(f["amps"])]}
            for k, f in FAMILIES.items()
        ],
        "colors": [{"key": k, **v} for k, v in COLORS.items()],
        "max_fuses": MAX_FUSES,
        "max_role": MAX_ROLE,
    }
