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
import os

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

# Amp -> colour, per family. Read off the reference chart; families that
# genuinely share a table share it here rather than repeating it, so a
# correction lands in one place.
#
# Mini and low-profile mini STOP AT 30A. Only the standard blade goes on to
# 35A and 40A -- a 40A mini does not exist, and offering one would be offering
# a part nobody can buy.
_STANDARD = {2: "gray", 3: "violet", 4: "pink", 5: "tan", 7.5: "brown",
             10: "red", 15: "blue", 20: "yellow", 25: "clear", 30: "green",
             35: "bluegreen", 40: "orange"}
_MINI = {a: c for a, c in _STANDARD.items() if a <= 30}
_MICRO = {5: "tan", 7.5: "brown", 10: "red", 15: "blue", 20: "yellow",
          25: "clear", 30: "green"}

# Maxi: the chart's ratings, plus the ones it does not draw but that are real
# and do not conflict with it. Note 15A and 60A are both blue -- which is why
# nothing here ever reads a rating back OUT of a colour.
_MAXI = {15: "blue", 20: "yellow", 30: "green", 35: "brown", 40: "orange",
         50: "red", 60: "blue", 70: "tan", 80: "clear", 100: "violet",
         120: "purple"}

# The cartridge pair. Same ratings, same colours, different housing.
_CASE = {20: "yellow", 30: "green", 40: "orange", 50: "red", 60: "blue"}

# Glass is not colour coded: the body is clear and the rating is printed on
# the end cap, which is what a rider reads.
_AGC = {a: None for a in (0.5, 1, 2, 3, 4, 5, 7.5, 10, 15, 20, 25, 30)}

# Ceramic tube (SFE): a white body with a coloured band. 5A and 20A are both
# yellow on the chart; that is the chart, and it is harmless because the
# colour is only ever derived from the rating.
_SFE = {1: "clear", 2: "pink", 3: "violet", 5: "yellow", 7.5: "brown",
        10: "red", 15: "blue", 20: "yellow", 30: "green"}

# Ceramic torpedo (European / Bosch). The classic set is 5, 8, 16, 25, 40;
# the longer series adds the rest. 25A is drawn blue here -- it appears blue
# on the labelled chart, though a white 25A torpedo also exists in a different
# length, so this one is worth checking against the part in your hand.
_TORPEDO = {3: "yellow", 5: "clear", 8: "clear", 10: "red", 15: "blue",
            16: "red", 20: "yellow", 25: "blue", 30: "blue", 40: "gray",
            50: "green"}

FAMILIES = {
    "micro2":  {"name": "Micro2", "codes": "APT, ATR", "mm": "9.1 x 3.8 x 15.3",
                "shape": "blade", "legs": 2, "amps": _MICRO},
    "micro3":  {"name": "Micro3", "codes": "ATL", "mm": "14.4 x 4.2 x 18.1",
                "shape": "blade", "legs": 3, "amps": _MICRO},
    "lpmini":  {"name": "Low-profile mini", "codes": "APS, ATT", "mm": "10.9 x 3.81 x 8.73",
                "shape": "blade", "legs": 2, "amps": _MINI},
    "mini":    {"name": "Mini", "codes": "APM, ATM", "mm": "10.9 x 3.6 x 16.3",
                "shape": "blade", "legs": 2, "amps": _MINI},
    "regular": {"name": "Standard", "codes": "ATO, ATC, APR, ATS", "mm": "19.1 x 5.1 x 18.5",
                "shape": "blade", "legs": 2, "amps": _STANDARD},
    "maxi":    {"name": "Maxi", "codes": "APX", "mm": "29.2 x 8.5 x 34.3",
                "shape": "blade", "legs": 2, "amps": _MAXI},
    "jcase":   {"name": "JCASE", "codes": "JCASE, Female Maxi", "mm": "",
                "shape": "case", "legs": 2, "amps": _CASE},
    "mcase":   {"name": "MCASE", "codes": "MCASE", "mm": "",
                "shape": "case", "legs": 2, "amps": _CASE},
    "glass":   {"name": "Glass tube", "codes": "AGC, AGU", "mm": "",
                "shape": "glass", "legs": 0, "amps": _AGC},
    "sfe":     {"name": "Ceramic tube", "codes": "SFE", "mm": "",
                "shape": "sfe", "legs": 0, "amps": _SFE},
    "torpedo": {"name": "Ceramic torpedo", "codes": "European, Bosch, GBC", "mm": "",
                "shape": "torpedo", "legs": 0, "amps": _TORPEDO},
}

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
    "regularblade": "regular", "standardblade": "regular",
    "lowprofile": "lpmini", "lowprofilemini": "lpmini", "miniloprofile": "lpmini",
    "lpmini": "lpmini", "micro": "micro2",
    "agc": "glass", "agu": "glass", "glasstube": "glass",
    "ceramictube": "sfe", "sfe": "sfe",
    "ceramic": "torpedo", "bosch": "torpedo", "continental": "torpedo",
    "european": "torpedo", "gbc": "torpedo", "ceramictorpedo": "torpedo",
    "femalemaxi": "jcase",
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

    # Commas separate the fuses, but riders write lists with commas too:
    # "headlight, horn: mini 10A" is ONE fuse that protects two things. So a
    # piece with no fuse in it is not an error yet -- it is the start of the
    # next fuse's role. It is only an error if no fuse ever claims it.
    out, pending = [], []          # pending: [(text, the error it raised)]
    for piece in str(value).split(","):
        piece = piece.strip()
        if not piece:
            continue
        if ":" in piece:
            # The fuse half never has a colon in it, so split at the last one
            # and let what it protects say whatever it likes.
            role, _, chunk = piece.rpartition(":")
            role = ", ".join([t for t, _ in pending] + [role.strip()]).strip(", ")
            pending = []
            if len(role) > MAX_ROLE:
                raise FuseError(f"what a fuse protects is limited to {MAX_ROLE} characters")
            family, amp = parse(chunk.strip())
            out.append((role or None, family, amp))
            continue
        try:
            family, amp = parse(piece)
        except FuseError as e:
            pending.append((piece, e))
            continue
        if pending:
            # "headlight, mini 10A": a role with no colon before its fuse.
            raise pending[0][1]
        out.append((None, family, amp))
    if pending:
        raise pending[0][1]
    if not out:
        raise FuseError("a fuse needs a size and a rating")
    if len(out) > MAX_FUSES:
        raise FuseError(f"at most {MAX_FUSES} fuses on one spec")
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


_IMG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "static", "img", "fuses")


def _image(family, amp):
    """The URL of this exact fuse's photograph, or None.

    Not everything is pictured: the chart draws Maxi from 20A to 80A, and
    only the classic five torpedoes. Those ratings are real and stay in the
    table -- the page falls back to the drawn fuse for them rather than
    dropping them. Most photos are PNGs cut from the chart; the Mini set is
    a sharper WebP set that replaced those.
    """
    for ext in ("webp", "png"):
        name = f"{family}-{_amp_text(amp)}.{ext}"
        if os.path.exists(os.path.join(_IMG_DIR, name)):
            return f"/img/fuses/{name}"
    return None


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
                       "color_name": COLORS[f["amps"][a]]["name"] if f["amps"][a] else None,
                       "img": _image(k, a)}
                      for a in sorted(f["amps"])]}
            for k, f in FAMILIES.items()
        ],
        "colors": [{"key": k, **v} for k, v in COLORS.items()],
        "max_fuses": MAX_FUSES,
        "max_role": MAX_ROLE,
    }
