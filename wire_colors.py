"""
Wire colours as a value type, not as free text.

A wire colour is a base colour and up to two stripes. Typed as text, one wire
comes back as "yellow w/ red", "Yel/Red", "Y/R" and "yellow red stripe" from
four different riders, and none of them sort, match or compare.

Stored canonically as colour keys -- "yellow/red" -- and rendered in whatever
abbreviation the bike's own manufacturer prints on its wiring diagram. Honda
calls blue Bu, Kawasaki calls it BL, Harley calls it BE. The rider is reading
their own manual, so the page should speak their manual's language while the
database keeps one form.

Each abbreviation table was read off that manufacturer's own wiring diagram
legend; the source is noted above each one, and a colour a legend does not
define is shown by its plain name rather than a code made up for it.
"""

# Ordered as the picker shows them. The hex is for the swatch, `light` says
# whether the jacket needs a darker outline to be visible on a pale page.
COLORS = {
    "black":      {"name": "Black",       "hex": "#1F1F22", "light": False},
    "white":      {"name": "White",       "hex": "#F4F4F1", "light": True},
    "red":        {"name": "Red",         "hex": "#D42B23", "light": False},
    "blue":       {"name": "Blue",        "hex": "#1F5FBF", "light": False},
    "lightblue":  {"name": "Light blue",  "hex": "#6FB5E8", "light": True},
    "green":      {"name": "Green",       "hex": "#1E8A3C", "light": False},
    "lightgreen": {"name": "Light green", "hex": "#8ED66F", "light": True},
    "yellow":     {"name": "Yellow",      "hex": "#F3D22B", "light": True},
    "brown":      {"name": "Brown",       "hex": "#6E3F1E", "light": False},
    "orange":     {"name": "Orange",      "hex": "#F07A1C", "light": False},
    "pink":       {"name": "Pink",        "hex": "#F09AB8", "light": True},
    "gray":       {"name": "Gray",        "hex": "#8E9096", "light": False},
    "purple":     {"name": "Purple",      "hex": "#6A3FA6", "light": False},
    "tan":        {"name": "Tan",         "hex": "#C9A66B", "light": True},
}

# ---------------------------------------------------------------------------
# What each manufacturer prints on its own wiring diagrams.
#
# Every table below was read off a real diagram legend (the source is on the
# line above it). A table lists only the colours that legend defines: where a
# make has no code for a colour -- Honda never prints purple, Ducati has no
# light green -- the page shows the plain colour name instead of a code
# invented for it. A make with no table at all shows plain names for every
# colour; those makes are listed at the end and want a legend from a manual.
#
# One table per make. Where a factory changed its scheme over the years the
# current one is here and the old one is noted, because the site cannot yet
# pick a table by model year:
#   KTM      pre-2008 English manuals used German codes (sw black, ws white,
#            rt red, bl BLUE, ge yellow, gn green, br brown, gr grey, vi violet)
#            -- note that "bl" flipped meaning when the scheme changed.
#   Moto Guzzi  pre-2000 diagrams spell colours out in Italian (Nero, Rosso,
#            Bianco ...) with no abbreviations at all.
#   Husqvarna  Cagiva- and BMW-era manuals (to 2013) are not covered; the
#            KTM-era bikes use the KTM key.
#   Indian   Springfield-era diagrams carry no codes.
# ---------------------------------------------------------------------------
ABBR = {
    # Honda wiring diagram key (power equipment and motorcycle manuals share it).
    # Black is "Bl", not "B" -- the mockup had that wrong.
    "Honda": {"black": "Bl", "white": "W", "red": "R", "blue": "Bu",
              "lightblue": "Lb", "green": "G", "lightgreen": "Lg",
              "yellow": "Y", "brown": "Br", "orange": "O", "pink": "P",
              "gray": "Gr"},
    # Yamaha 2012 TMAX XP500A service manual, "Color code". Also defines
    # Ch Chocolate and Dg Dark green, which are not on the picker.
    "Yamaha": {"black": "B", "white": "W", "red": "R", "blue": "L",
               "lightblue": "Sb", "green": "G", "lightgreen": "Lg",
               "yellow": "Y", "brown": "Br", "orange": "O", "pink": "P",
               "gray": "Gy"},
    # Kawasaki service manuals, wiring diagram key. Also CH Chocolate, DG Dark
    # green.
    "Kawasaki": {"black": "BK", "white": "W", "red": "R", "blue": "BL",
                 "lightblue": "LB", "green": "G", "lightgreen": "LG",
                 "yellow": "Y", "brown": "BR", "orange": "O", "pink": "P",
                 "gray": "GY", "purple": "PU"},
    # Suzuki DL650 service manual, "Wire color" (V Violet from the GSX-R
    # manuals; Dg Dark green not on the picker).
    "Suzuki": {"black": "B", "white": "W", "red": "R", "blue": "Bl",
               "lightblue": "Lbl", "green": "G", "lightgreen": "Lg",
               "yellow": "Y", "brown": "Br", "orange": "O", "pink": "P",
               "gray": "Gr", "purple": "V"},
    # Harley-Davidson wiring diagrams, Table "Wire Color Codes" (2008 XB
    # diagnostics manual, same table as the H-D books) plus LBE for light
    # blue as printed on the 2023 FLTRKSE diagrams. Striped: GN/Y.
    "Harley-Davidson": {"black": "BK", "white": "W", "red": "R", "blue": "BE",
                        "lightblue": "LBE", "green": "GN", "lightgreen": "LGN",
                        "yellow": "Y", "brown": "BN", "orange": "O",
                        "pink": "PK", "gray": "GY", "purple": "V", "tan": "TN"},
    # Buell 2008 XB Electrical Diagnostics Manual, Table B-2 (a Harley table;
    # it has no light blue).
    "Buell": {"black": "BK", "white": "W", "red": "R", "blue": "BE",
              "green": "GN", "lightgreen": "LGN", "yellow": "Y", "brown": "BN",
              "orange": "O", "pink": "PK", "gray": "GY", "purple": "V",
              "tan": "TN"},
    # Ducati Scrambler 800 workshop manual, "Wire colour coding".
    "Ducati": {"black": "Bk", "white": "W", "red": "R", "blue": "B",
               "lightblue": "Lb", "green": "G", "yellow": "Y", "brown": "Bn",
               "orange": "O", "pink": "P", "gray": "Gr", "purple": "V"},
    # Aprilia RSV 1000 Tuono R manual, "Cable colours" -- Italian initials:
    # N nero, Bi bianco, R rosso, B blu, Az azzurro, V verde, G giallo,
    # M marrone, Ar arancio, Ro rosa, Gr grigio, Vi viola. Note V is GREEN
    # and G is YELLOW here.
    "Aprilia": {"black": "N", "white": "Bi", "red": "R", "blue": "B",
                "lightblue": "Az", "green": "V", "yellow": "G", "brown": "M",
                "orange": "Ar", "pink": "Ro", "gray": "Gr", "purple": "Vi"},
    # Moto Guzzi V85 TT and Griso manuals: the same key as Aprilia.
    "Moto Guzzi": {"black": "N", "white": "Bi", "red": "R", "blue": "B",
                   "lightblue": "Az", "green": "V", "yellow": "G", "brown": "M",
                   "orange": "Ar", "pink": "Ro", "gray": "Gr", "purple": "Vi"},
    # Vespa Primavera 50 / GTS and Gilera Nexus 500 manuals: Piaggio's
    # two-letter Italian key. Ne nero, Bi bianco, Rs rosso, Bl blu, Az azzurro,
    # Ve verde, Gi giallo, Ma marrone, Ar arancio, Ro rosa, Gr grigio, Vi viola.
    "Vespa": {"black": "Ne", "white": "Bi", "red": "Rs", "blue": "Bl",
              "lightblue": "Az", "green": "Ve", "yellow": "Gi", "brown": "Ma",
              "orange": "Ar", "pink": "Ro", "gray": "Gr", "purple": "Vi"},
    "Piaggio": {"black": "Ne", "white": "Bi", "red": "Rs", "blue": "Bl",
                "lightblue": "Az", "green": "Ve", "yellow": "Gi", "brown": "Ma",
                "orange": "Ar", "pink": "Ro", "gray": "Gr", "purple": "Vi"},
    "Gilera": {"black": "Ne", "white": "Bi", "red": "Rs", "blue": "Bl",
               "lightblue": "Az", "green": "Ve", "yellow": "Gi", "brown": "Ma",
               "orange": "Ar", "pink": "Ro", "gray": "Gr", "purple": "Vi"},
    # Derbi GPR 125 4T workshop manual key. G is GRAY and GR is GREEN here.
    "Derbi": {"black": "B", "white": "W", "red": "R", "blue": "BL",
              "lightblue": "SBL", "green": "GR", "yellow": "Y", "brown": "BR",
              "orange": "O", "pink": "P", "gray": "G", "purple": "VI"},
    # MV Agusta Brutale Oro/S workshop manual, "Wiring colour code".
    # Striped: Br/Bk.
    "MV Agusta": {"black": "Bk", "white": "W", "red": "R", "blue": "B",
                  "lightblue": "Sb", "green": "G", "yellow": "Y", "brown": "Br",
                  "orange": "O", "pink": "P", "gray": "Gr", "purple": "V"},
    # Triumph: the Lucas / BS AU 7 letters, used at Meriden and still the
    # "Key to wiring colour codes" in Hinckley manuals. U blue, N brown,
    # K pink, S slate (grey), P purple; a second letter is the tracer.
    "Triumph": {"black": "B", "white": "W", "red": "R", "blue": "U",
                "green": "G", "lightgreen": "LG", "yellow": "Y", "brown": "N",
                "orange": "O", "pink": "K", "gray": "S", "purple": "P"},
    # KTM wiring diagrams (current English key, lower case; striped re-wh).
    # Husqvarna 2014- and GasGas 2021- manuals are KTM manuals.
    "KTM": {"black": "bl", "white": "wh", "red": "re", "blue": "bu",
            "green": "gn", "yellow": "ye", "brown": "br", "orange": "or",
            "gray": "gr", "purple": "pu"},
    "Husqvarna": {"black": "bl", "white": "wh", "red": "re", "blue": "bu",
                  "green": "gn", "yellow": "ye", "brown": "br", "orange": "or",
                  "gray": "gr", "purple": "pu"},
    # No legend checked yet -- plain colour names until one is:
    #   Indian (Polaris era), Victory, Benelli, Fantic, GasGas (pre-2021).
}


def label(make, key):
    """The manufacturer's code for a colour, or its plain name where that
    manufacturer's legend has no code for it."""
    return ABBR.get(make or "", {}).get(key) or COLORS[key]["name"]


MAX_STRIPES = 2          # base + 2 stripes; second is rare but real on BMW/Ducati
MAX_WIRES = 8            # a stator is 3, an R/R connector 5, a switch block up to 8
MAX_ROLE = 40


class WireColorError(ValueError):
    """A value that is not a wire colour this vocabulary can express."""


def _slug(part):
    """Fold a written colour onto its key, so an existing free-text value can
    be adopted rather than thrown away: 'Light Blue' and 'lightblue' are one."""
    return "".join(ch for ch in part.lower() if ch.isalnum())


_BY_SLUG = {_slug(k): k for k in COLORS}
_BY_SLUG.update({_slug(v["name"]): k for k, v in COLORS.items()})


def parse(value):
    """Canonical key list for a wire colour, or raise.

    Accepts what people already typed -- "Yellow/Red", "yellow / red" -- so the
    values on the site before this existed do not need re-entering.
    """
    if value is None or not str(value).strip():
        raise WireColorError("a wire colour needs at least a base colour")
    parts = [p.strip() for p in str(value).split("/") if p.strip()]
    if not parts:
        raise WireColorError("a wire colour needs at least a base colour")
    if len(parts) > 1 + MAX_STRIPES:
        raise WireColorError(
            f"at most a base colour and {MAX_STRIPES} stripes")
    keys = []
    for part in parts:
        key = _BY_SLUG.get(_slug(part))
        if key is None:
            raise WireColorError(
                f"{part!r} is not a colour on the list "
                f"({', '.join(COLORS[k]['name'] for k in COLORS)})")
        keys.append(key)
    if len(set(keys)) != len(keys):
        raise WireColorError("a stripe cannot repeat the colour under it")
    return keys


def normalise(value):
    """The form that goes in the database: 'yellow/red'."""
    return "/".join(parse(value))


def abbreviate(value, make=None):
    """The form the rider sees, in their own manufacturer's shorthand."""
    return "/".join(label(make, k) for k in parse(value))


def describe(value):
    """Plain words, for a screen reader and for anyone who does not read the
    abbreviations: 'Yellow / red stripe'."""
    keys = parse(value)
    out = COLORS[keys[0]]["name"]
    for k in keys[1:]:
        out += " / " + COLORS[k]["name"].lower() + " stripe"
    return out


# --------------------------------------------------------------------------
# A spec can be more than one wire.
#
# A kickstand switch has two. Recorded as one wire each they become two specs,
# or -- as actually happened here -- two "alternates" competing for votes when
# both are correct and neither replaces the other. A wire colour value is
# therefore a LIST of wires, written comma-separated:
#
#     green/white, green/black
#
# with an optional role per wire, which is what tells two otherwise similar
# wires apart and becomes necessary once there are more than two:
#
#     to switch: green/white, to harness: green/black
#
# One wire is a list of one, so every value written before this still parses
# and still means the same thing.
# --------------------------------------------------------------------------

def parse_set(value):
    """[(role_or_None, [colour keys]), ...] for a wire colour value."""
    if value is None or not str(value).strip():
        raise WireColorError("a wire colour needs at least a base colour")
    chunks = [c.strip() for c in str(value).split(",") if c.strip()]
    if not chunks:
        raise WireColorError("a wire colour needs at least a base colour")
    if len(chunks) > MAX_WIRES:
        raise WireColorError(f"at most {MAX_WIRES} wires on one spec")

    out = []
    for chunk in chunks:
        role = None
        if ":" in chunk:
            role, chunk = chunk.split(":", 1)
            role, chunk = role.strip(), chunk.strip()
            if not role:
                role = None
            elif len(role) > MAX_ROLE:
                raise WireColorError(
                    f"a wire's role is limited to {MAX_ROLE} characters")
        out.append((role, parse(chunk)))

    roles = [r for r, _ in out if r]
    if len(set(roles)) != len(roles):
        raise WireColorError("two wires cannot share the same role")
    return out


def normalise_set(value):
    """The form that goes in the database."""
    return ", ".join(
        (f"{role}: " if role else "") + "/".join(keys)
        for role, keys in parse_set(value))


def describe_set(value, make=None):
    """One readable line per wire, for a page or a screen reader."""
    out = []
    for role, keys in parse_set(value):
        abbr = "/".join(label(make, k) for k in keys)
        name = COLORS[keys[0]]["name"] + "".join(
            " / " + COLORS[k]["name"].lower() + " stripe" for k in keys[1:])
        out.append({"role": role, "value": "/".join(keys),
                    "abbr": abbr, "name": name})
    return out


def vocabulary(make=None):
    """Everything a client needs to render and offer wire colours."""
    return {
        "colors": [{"key": k, "name": v["name"], "hex": v["hex"],
                    "light": v["light"], "abbr": label(make, k)}
                   for k, v in COLORS.items()],
        "make": make,
        "legend": make in ABBR,
        "max_stripes": MAX_STRIPES,
        "max_wires": MAX_WIRES,
        "max_role": MAX_ROLE,
    }
