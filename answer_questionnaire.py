"""Answer the Spec Tree questionnaire for every bike that has no answers yet.

    py answer_questionnaire.py [--dry-run] [--make <Make>] <workbook.xlsx> [...]

A bike with no questionnaire answers has only the six universal fields, so
its page looks empty. This walks the same questionnaire the wizard does
(questionnaire.run), with answers worked out from what is known about each
bike -- make, model name, era, engine description, displacement, category --
and creates the fields those answers trigger, exactly as
POST /api/questionnaire/<id>/build would. The answers are saved in
bike_answers with answered_by NULL, so the wizard shows them pre-filled and
the bike's manager can confirm or change them; re-running the wizard only
ever adds fields, so a wrong provisional answer costs an empty field, not a
value.

Where the facts come from:
  imported bikes  -> the model-list workbooks (Engine, Stroke, Category,
                     Notes columns), matched by make, model code and year
  Honda catalog   -> the model code (CB, CBR, VT, GL, XL ...) and the
                     catalog's own Drive Type spec
Electric bikes are skipped: the questionnaire has no path for them.

The rules are era-and-type rules of thumb (a 1970s Japanese four has points
until about 1980, a sportbike from 1995 on has two headlights, a motocrosser
has no battery ...). They are provisional by design. "Not 100% sure" is used
only where nothing sensible can be said, because every such answer lands in
the admin's not-sure queue.
"""
import datetime
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter, defaultdict

import questionnaire
from import_model_lists import read_rows, resolve_codes, strip_years, THIS_YEAR

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data.db")

JAPANESE = {"Honda", "Kawasaki", "Suzuki", "Yamaha"}
ITALIAN = {"Aprilia", "Benelli", "Ducati", "Fantic", "Gilera", "Moto Guzzi",
           "MV Agusta", "Piaggio", "Vespa"}
KTM_GROUP = {"KTM", "Husqvarna", "GasGas"}
SCOOTER_MAKES = {"Piaggio", "Vespa"}

TYPE_LETTER = {"Dirt bike / Off-road": "A", "Cruiser": "B", "Street bike / Sport bike": "C",
               "Touring": "D", "Dual-sport / Adventure": "E", "Scooter": "F",
               "Standard / Naked": "G", "Harley-Davidson": "H"}


def has(text, *words):
    text = text.lower()
    return any(w.lower() in text for w in words)


# ---------------------------------------------------------------------------
# Features: one flat dict per bike, the same shape whichever source it came from
# ---------------------------------------------------------------------------
def base_features(make, model, y0, y1, cc, engine, stroke, category, notes, bike_type):
    last = THIS_YEAR if y1 is None else y1
    cat = (category or "").lower()
    f = {
        "make": make, "model": model, "y0": y0, "y1": y1, "last": last,
        "ref": (y0 + last) // 2, "cc": cc or 0, "engine": (engine or "").lower(),
        "stroke": stroke, "cat": cat, "notes": (notes or "").lower(),
        "bike_type": bike_type,
    }
    f["mx"] = has(cat, "motocross", "minicross")
    f["enduro"] = has(cat, "enduro", "cross-country")
    f["trials"] = "trials" in cat
    f["dual"] = has(cat, "dual-sport", "adventure", "supermoto", "rally", "trail")
    f["scooter"] = has(cat, "scooter", "underbone")
    f["moped"] = "moped" in cat
    f["mini"] = "mini" in cat and not f["mx"]
    f["cruiser"] = has(cat, "cruiser", "bobber", "chopper", "bagger", "muscle")
    f["touring"] = "touring" in cat
    f["sport"] = has(cat, "sport", "superbike", "supersport", "racer") and not f["touring"] and not f["dual"]
    f["racer"] = has(cat, "racer", "race", "track") and not f["dual"]
    f["retro"] = has(cat, "retro", "classic")
    f["pure_offroad"] = (f["mx"] or f["enduro"] or f["trials"] or "off-road" in cat) and not f["dual"]
    f["offroad_mini"] = f["mini"] and f["cc"] < 100
    f["road"] = not (f["pure_offroad"] or f["offroad_mini"] or f["racer"])
    f["japanese"] = make in JAPANESE
    f["harley"] = make in ("Harley-Davidson", "Buell")
    f["polaris"] = make == "Victory" or (make == "Indian" and y0 >= 2011)
    f["meriden"] = make == "Triumph" and y0 <= 1983
    f["hinckley"] = make == "Triumph" and y0 >= 1990
    return f


def parse_engine(f):
    """Cylinders, arrangement and cooling out of the engine description."""
    model = f["model"].lower()
    # "four-stroke", "four-valve", "16-valve" are not cylinder counts.
    e = re.sub(r"\b(two|four|2|4)[- ]?(stroke|valve|valves)\b|\b\d+[- ]?valves?\b", " ", f["engine"])
    cyl = None
    if e.strip().startswith("single"):
        cyl = 1
    elif has(e, "six", "inline-6", "flat-six", "6-cyl"):
        cyl = 6
    elif has(e, "v4", "v-4", "v-four", "four", "inline-4", "flat-four", "4-cyl", "in-line four"):
        cyl = 4
    elif has(e, "triple", "three", "inline-3", "3-cyl"):
        cyl = 3
    elif has(e, "twin", "boxer", "2-cyl", "parallel", "l-twin", "flat-twin"):
        cyl = 2
    elif has(e, "single", "1-cyl", "monocylind"):
        cyl = 1
    f["cyl"] = cyl

    arr = None
    if cyl in (2, 4, 6):
        if f["make"] == "Moto Guzzi" or has(e, "boxer", "transverse", "opposed") or re.search(r"flat[- ]?(twin|four|six)", e):
            arr = "C"                      # left / right grouping
        elif has(e, "v-twin", "v4", "v-4", "v-four", "l-twin", "v twin", "vee", "-deg v", "° v", "v-six", "v6"):
            arr = "A"                      # front and rear
        elif re.search(r"\bv\b", e):
            arr = "A"
        else:
            arr = "B"                      # inline
    f["arr"] = arr

    if has(e, "liquid", "water", "coolant"):
        cool = "C"
    elif has(e, "oil-cooled", "air/oil", "air-oil", "oil/air", "oil cooled", "air-/oil"):
        cool = "B"
    elif has(e, "air"):
        cool = "A"
    else:
        cool = None
    if f["make"] == "Harley-Davidson" and f["ref"] >= 2014 and has(model, "ultra limited", "limited", "tri glide", "cvo"):
        cool = "C"                         # Twin-Cooled heads
    if cool is None:
        if f["stroke"] == 2 and (f["mx"] or f["enduro"] or f["trials"]) and f["ref"] >= 1984:
            cool = "C"
        elif f["stroke"] == 4 and f["sport"] and f["ref"] >= 1990:
            cool = "C"
        elif f["scooter"] and f["cc"] >= 125 and f["ref"] >= 2005:
            cool = "C"
        elif f["stroke"] == 2 and f["road"] and f["ref"] >= 1988 and f["cc"] >= 125 and not f["scooter"]:
            cool = "C"
        else:
            cool = "A"
    f["cool"] = cool

    if has(e, "dohc"):
        f["valves"] = "D"
    elif has(e, "desmo"):
        f["valves"] = "D"
    elif has(e, "sohc", "ohv", "pushrod", "ohc", "side-valve", "flathead", "ioe", "f-head"):
        f["valves"] = "C"
    else:
        f["valves"] = None

    f["shaft"] = has(e, "shaft") or has(f["notes"], "shaft drive", "shaft-drive")
    f["belt"] = (has(e, "belt") and not has(e, "cam", "desmo", "timing")) or has(f["notes"], "belt final", "belt drive", "belt-drive")
    f["fi_text"] = has(e, "fuel injection", "fuel-injected", "injected", "efi", "tpi", "tbi", " fi ", "-fi")
    f["carb_text"] = has(e, "carb")


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------
def fuel_injected(f):
    make, m, ref, cc = f["make"], f["model"].lower(), f["ref"], f["cc"]
    if f["fi_text"]:
        return True
    if f["carb_text"]:
        return False
    if f["stroke"] == 2:
        if make in KTM_GROUP and (f["enduro"] or f["dual"]) and ref >= 2018:
            return True                    # TPI / TBI
        if make in KTM_GROUP and f["mx"] and ref >= 2023:
            return True
        if has(m, "purejet", "pure jet", "ditech", "di-tech"):
            return True
        return False
    if has(m, "v-star 650", "xvs650", "v-star 250", "xv250", "virago", "royal star", "venture",
           "concours", "gtr1000", "zg1000", "dr-z", "dr650", "dr 650", "dr200", "tw200",
           "klr650", "ninja 250", "ex250", "gs500", "w650", "bandit 1200", "bandit 600",
           "950 adventure", "950 super", "950 supermoto", "s1 lightning", "m2 cyclone", "blast",
           "eliminator", "vulcan 800", "vulcan 750", "vn750", "vn800", "en500", "zrx", "kz",
           "gs1000", "gsx-r750 (air", "xs650", "xs750", "xs850", "xs1100", "xs400", "xs500", "xs360",
           "xj550", "xj650", "xj700", "xj750", "xj900", "xj600", "fj600", "fj1100", "fj1200",
           "fzr", "fz750", "cb750", "cbx"):
        if ref < 2015:
            return False
    if has(m, "tl1000", "gsx1400", "hayabusa", "v-rod", "vrsc", "xb", "x1 lightning", "1125",
           "rsv", "tuono", "f4", "brutale", "panigale", "monster 1200", "multistrada", "diavel",
           "z125 pro", "grom", "fjr", "vmax (2009", "fz1", "fz6", "mt-", "tracer", "xsr", "tenere 700",
           "super tenere", "z1000", "z900", "z800", "z650", "z400", "ninja 400", "ninja 650",
           "versys", "vulcan 900", "vulcan 1700", "vulcan s", "sv650", "v-strom", "burgman",
           "gsx-s", "gsx-r1000", "gsx-r600", "gsx-r750 (liquid", "boulevard", "rocket",
           "speed triple 1050", "street triple", "daytona 675", "tiger 800", "tiger 1200",
           "tiger 1050", "explorer", "955i", "t595", "t509", "tt600", "bolt", "raider", "roadliner",
           "stratoliner", "v-star 950", "v-star 1300", "wr250r", "x-max", "xmax", "nmax", "tmax",
           "gts", "mp3", "beverly", "nexus", "gp 800", "fuoco") or re.search(r"\d\s?i\.?e\.?\b", m):
        return True                        # 907ie, 900 i.e.
    if f["harley"]:
        if make == "Buell":
            return ref >= 2003
        return ref >= 2007
    if f["polaris"]:
        return True
    if make == "Indian":
        return ref >= 2009                 # Kings Mountain PowerPlus 105
    if make == "Ducati":
        return ref >= 2000 or (cc >= 850 and ref >= 1988 and f["sport"])
    if make == "Moto Guzzi":
        return (cc >= 1000 and ref >= 1994) or (cc < 1000 and ref >= 2003)
    if f["hinckley"]:
        if has(m, "bonneville", "thruxton", "scrambler", "america", "speedmaster") and ref < 2008:
            return False
        return ref >= 2005
    if f["meriden"]:
        return False
    if make == "MV Agusta":
        return f["y0"] >= 1997
    if make == "Aprilia":
        return ref >= 2000
    if make == "Benelli":
        return ref >= 2003
    if make in ("Piaggio", "Vespa", "Gilera", "Derbi"):
        return ref >= 2010 if f["scooter"] else ref >= 2006
    if make == "Fantic":
        return ref >= 2015
    if make == "KTM":
        if f["mx"] or f["enduro"] or f["pure_offroad"]:
            return ref >= 2011
        return ref >= 2008
    if make == "Husqvarna":
        return ref >= 2008
    if make == "GasGas":
        return ref >= 2011 if not f["trials"] else False
    # Japanese and the rest, by type
    if f["scooter"]:
        return ref >= 2008 if cc >= 100 else ref >= 2018
    if f["mx"]:
        return ref >= 2010
    if f["enduro"]:
        return ref >= 2012
    if f["pure_offroad"] or f["offroad_mini"]:
        return ref >= 2020
    if f["mini"]:
        return ref >= 2014
    if f["dual"]:
        return ref >= 2010 if cc < 700 else ref >= 1999
    if f["cruiser"]:
        return ref >= 2007
    if f["touring"]:
        return ref >= 1998
    if f["sport"]:
        return ref >= 2002 if cc >= 590 else ref >= 2008
    return ref >= 2007


def electronic_ignition(f):
    make, ref = f["make"], f["ref"]
    if ref < 1925:
        return False
    if f["stroke"] == 2:
        return ref >= (1975 if (f["mx"] or f["enduro"] or f["trials"] or f["pure_offroad"]) else 1978)
    if make == "Harley-Davidson":
        return ref >= 1979
    if make == "Triumph":
        return ref >= 1979
    if make in ITALIAN:
        return ref >= 1982
    if make == "Indian":
        return ref >= 1999
    return ref >= 1980


def battery(f):
    ref = f["ref"]
    if f["ref"] < 1925 or f["moped"]:
        return False
    if f["trials"] or f["offroad_mini"] or f["racer"]:
        return False
    if f["mx"]:
        if f["make"] in KTM_GROUP:
            return ref >= 2018 or (f["stroke"] == 4 and ref >= 2011)
        return f["stroke"] == 4 and ref >= 2018
    if f["enduro"] or ("off-road" in f["cat"] and not f["dual"]):
        if f["make"] in KTM_GROUP:
            return ref >= 2008 if f["stroke"] == 2 else ref >= 1998
        return ref >= 2015 if f["stroke"] == 2 else ref >= 1998
    return True


def electric_start(f):
    make, m, ref, cc = f["make"], f["model"].lower(), f["ref"], f["cc"]
    if f["mx"] or f["enduro"] or "off-road" in f["cat"]:
        return True                        # only asked when there is a battery
    if make == "Harley-Davidson":
        return ref >= 1974 or (has(m, "fl", "electra") and ref >= 1965)
    if make == "Buell" or f["polaris"]:
        return True
    if make == "Indian":
        return ref >= 1999
    if make == "Triumph":
        return ref >= 1980
    if make == "Vespa":
        return ref >= 1998
    if make == "Moto Guzzi":
        return ref >= 1967 if cc >= 500 else ref >= 1985
    if make in ITALIAN:
        return ref >= 1975 if f["stroke"] == 4 else ref >= 1990
    if f["scooter"]:
        return ref >= 1985
    if f["stroke"] == 2:
        return ref >= 1990
    if f["dual"] and cc < 700:
        return ref >= 1992
    if cc < 250:
        return ref >= 1985
    return ref >= 1972


def turn_signals(f):
    if f["enduro"] and not f["mx"]:
        return f["ref"] >= 1990
    if not f["road"] or f["moped"]:
        return False
    return f["ref"] >= 1972


def headlight(f):
    if f["mx"] or f["trials"] or f["offroad_mini"] or f["racer"]:
        return False
    if "off-road" in f["cat"] and not f["dual"]:
        return False
    return True


def two_headlights(f):
    return f["sport"] and f["ref"] >= 1995 and f["cc"] >= 400 and f["stroke"] == 4


def parking_light(f):
    if not f["road"] or f["harley"] or f["make"] in ("Indian", "Victory"):
        return False
    return f["ref"] >= 1980


def drive(f):
    make, m, ref = f["make"], f["model"].lower(), f["ref"]
    if f["shaft"]:
        return "C"
    if f["belt"]:
        return "B"
    if make == "Vespa" and f["stroke"] == 2 and not has(m, "et2", "et4"):
        return "D"                         # gear drive in the hub: no option fits
    if f["scooter"] or f["moped"]:
        return "B"                         # CVT belt
    if make == "Harley-Davidson":
        return "B" if (ref >= 1992 or (ref >= 1985 and not has(m, "xl", "sportster"))) else "A"
    if make == "Buell" or f["polaris"]:
        return "B"
    if make == "Moto Guzzi" and f["cyl"] == 2 and ref >= 1967:
        return "C"
    if f["cruiser"] and f["japanese"] and f["cc"] >= 600 and ref >= 1985:
        return "C" if ref < 2006 else "B"
    return "A"


def valvetrain(f):
    make, m, ref = f["make"], f["model"].lower(), f["ref"]
    if make == "Harley-Davidson":
        if ref < 1948:
            return "C"
        if has(m, "xl", "sportster", "ironhead") and ref < 1986:
            return "C"
        return "E"
    if make == "Buell" or f["polaris"]:
        return "E"
    if f["valves"]:
        return f["valves"]
    if make in KTM_GROUP:
        return "D"
    if f["hinckley"] or make == "Ducati":
        return "D"
    if f["meriden"] or make in ("Moto Guzzi", "Indian", "Vespa", "Piaggio", "Gilera", "Derbi"):
        return "C"
    if f["stroke"] == 4 and f["cc"] >= 600 and (f["sport"] or f["touring"] or "naked" in f["cat"]) and ref >= 1985:
        return "D"
    return "C"


def oil_layout(f):
    make, m = f["make"], f["model"].lower()
    if make == "Harley-Davidson":
        return "B" if has(m, "xl", "sportster", "xr1200", "street", "xg") else "A"
    if make == "Buell":
        return "B"
    if make == "Indian" and not f["polaris"]:
        return "A"
    if f["meriden"]:
        return "A"
    if make == "Moto Guzzi":
        return "B"
    if f["scooter"] and f["stroke"] == 4:
        return "B"
    return "C"


def two_stroke_lube(f):
    make, m, ref = f["make"], f["model"].lower(), f["ref"]
    if f["mx"] or f["trials"] or f["racer"]:
        return "B"
    if f["enduro"] or ("off-road" in f["cat"] and not f["dual"]):
        if make in KTM_GROUP and ref >= 2018:
            return "A"
        return "B"
    if make == "Vespa":
        return "A" if ref >= 1978 else "B"
    if make in JAPANESE:
        return "A" if ref >= 1966 else "B"
    return "A" if ref >= 1980 else "B"


def fuel_pump(f):
    """Only for carburetted bikes. (kind, has) where kind is A mechanical / B electric."""
    ref, cc = f["ref"], f["cc"]
    if f["harley"] or f["stroke"] == 2 and not f["scooter"]:
        return None
    if f["scooter"] and ref >= 1990 and not (f["make"] == "Vespa" and f["stroke"] == 2):
        return "A"
    if f["cruiser"] and f["japanese"] and ref >= 1985 and cc >= 500:
        return "A"
    if (f["sport"] or f["touring"]) and f["japanese"] and cc >= 750 and ref >= 1986:
        return "B"
    return None


def air_front(f):
    if f["japanese"] and f["road"] and 1980 <= f["ref"] <= 1986 and not f["scooter"] and f["cc"] >= 400:
        return True
    if f["make"] in KTM_GROUP and f["mx"] and f["ref"] >= 2017:
        return True
    return False


def air_rear(f):
    m = f["model"].lower()
    if f["make"] == "Harley-Davidson" and 1980 <= f["ref"] <= 2016 and has(m, "flh", "flt", "road king", "electra", "street glide", "road glide", "ultra", "tour glide"):
        return True
    if f["japanese"] and f["road"] and 1980 <= f["ref"] <= 1986 and not f["scooter"] and f["cc"] >= 400:
        return True
    return False


def hydraulic_clutch(f):
    make, m, ref, cc = f["make"], f["model"].lower(), f["ref"], f["cc"]
    if f["scooter"] or f["moped"]:
        return False
    if make == "Harley-Davidson":
        return ref >= 2014 and has(m, "flh", "flt", "road king", "electra", "street glide", "road glide", "ultra", "cvo")
    if make == "Victory":
        return True
    if make == "Ducati":
        return cc >= 800 and ref >= 1988
    if make == "Moto Guzzi":
        return cc >= 1000 and ref >= 1995
    if f["hinckley"]:
        return (cc >= 1050 and ref >= 2005) or has(m, "rocket")
    if make in KTM_GROUP:
        if f["mx"] or f["enduro"] or f["trials"] or f["pure_offroad"]:
            return ref >= 2000 and cc >= 60
        return cc >= 690 and not has(m, "790", "890", "990 duke")
    if make == "Aprilia":
        return has(m, "rsv mille", "rsv 1000", "rsv1000", "falco", "futura") or (cc >= 990 and ref < 2010)
    if make == "MV Agusta":
        return cc >= 900 and f["y0"] >= 1997
    if f["japanese"]:
        if has(m, "fjr", "vmax (2009", "mt-01", "xjr1300", "royal star", "venture", "v-star 1300", "raider",
               "roadliner", "stratoliner", "warrior", "gts1000", "zx-14", "zzr1200", "zx-12", "vulcan 15",
               "vulcan 16", "vulcan 20", "vulcan 17", "concours 14", "h2", "hayabusa", "gsx1400", "m109",
               "c90", "vl1500", "b-king"):
            return True
        return cc >= 1250 and ref >= 1990 and not f["cruiser"]
    return False


def oil_filter(f):
    make, ref = f["make"], f["ref"]
    if f["stroke"] == 2:
        return "A"
    if make == "Harley-Davidson":
        return "B" if ref >= 1940 else "D"
    if f["meriden"]:
        return "B" if ref >= 1979 else "D"
    if ref < 1970:
        return "D"
    if ref < 1980 and (f["cyl"] or 1) <= 2 and make not in ("Moto Guzzi", "Ducati"):
        return "D"
    return "B"


def front_disc(f):
    make, ref, cc = f["make"], f["ref"], f["cc"]
    if ref < 1970:
        return False
    if f["offroad_mini"] or f["moped"]:
        return False
    if f["mx"] or f["enduro"] or ("off-road" in f["cat"] and not f["dual"]):
        return ref >= 1984
    if f["trials"]:
        return ref >= 1988
    if make == "Vespa":
        return ref >= 1998
    if f["scooter"]:
        return ref >= 1995
    if f["mini"]:
        return ref >= 2000
    if f["dual"] or "trail" in f["cat"]:
        return ref >= 1986 and cc >= 200
    if f["harley"]:
        return ref >= 1973
    if make == "Moto Guzzi":
        return ref >= 1974
    if cc < 200:
        return ref >= 1982
    return ref >= 1973


def rear_disc(f):
    make, ref, cc = f["make"], f["ref"], f["cc"]
    if not front_disc(f):
        return False
    if f["mx"] or f["enduro"] or ("off-road" in f["cat"] and not f["dual"]):
        return ref >= 1987
    if f["trials"]:
        return ref >= 1990
    if make == "Vespa":
        return cc >= 250 and ref >= 2003
    if f["scooter"]:
        return cc >= 250 or ref >= 2012
    if f["mini"]:
        return ref >= 2010
    if f["dual"] or "trail" in f["cat"]:
        return ref >= 1995 and cc >= 250
    if make == "Harley-Davidson":
        return ref >= 1980
    if f["cruiser"]:
        return cc >= 800 or ref >= 2010
    if cc < 400:
        return ref >= 1990
    return ref >= 1978


def tubes(f):
    make, m, ref, cc = f["make"], f["model"].lower(), f["ref"], f["cc"]
    if f["mx"] or f["enduro"] or f["trials"] or f["pure_offroad"] or f["offroad_mini"] or f["moped"]:
        return True
    if f["dual"] or "trail" in f["cat"]:
        return not (cc >= 1000 and ref >= 2010)
    if make == "Vespa":
        return f["stroke"] == 2
    if f["scooter"]:
        return False
    if ref < 1978:
        return True
    if make == "Harley-Davidson":
        return has(m, "heritage", "springer", "road king", "deluxe", "slim", "wide glide", "classic",
                   "xl883", "883", "cross bones", "hydra")
    if f["cruiser"]:
        if f["japanese"]:
            return has(m, "classic", "heritage", "vintage", "deluxe", "drifter", "nomad", "silverado", "tourer")
        if make == "Indian":
            return has(m, "chief", "vintage", "springfield", "roadmaster")
        if make == "Moto Guzzi":
            return has(m, "california", "nevada")
        return False
    if f["retro"]:
        return not has(m, "xjr", "z900rs", "xsr", "z650rs", "cb1100", "street twin", "speed twin",
                       "thruxton rs", "gsx-s", "cb1000")
    if f["meriden"]:
        return True
    return False


def radiator_fan(f):
    if f["cool"] != "C":
        return False
    if f["mx"]:
        return False
    if f["enduro"] or f["trials"]:
        return f["ref"] >= 2012 if f["enduro"] else f["ref"] >= 2000
    if f["stroke"] == 2 and not f["scooter"]:
        return f["ref"] >= 1990
    return True


def dual_front_discs(f):
    make, m, ref, cc = f["make"], f["model"].lower(), f["ref"], f["cc"]
    if make == "Harley-Davidson":
        return has(m, "flh", "flt", "road king", "electra", "street glide", "road glide", "ultra",
                   "tour glide", "v-rod", "vrsc", "fat bob", "low rider s", "pan america") and ref >= 1980
    if f["scooter"]:
        return cc >= 400
    if f["cruiser"]:
        return cc >= 1600 and f["japanese"] or has(m, "road star", "royal star", "vmax", "raider", "m109", "vulcan 2000")
    if f["dual"]:
        return cc >= 800 and ref >= 1990
    if f["sport"]:
        return (ref >= 1985 and cc >= 500) or (f["stroke"] == 2 and cc >= 240 and ref >= 1988)
    if f["touring"]:
        return ref >= 1980 and cc >= 750
    return ref >= 1980 and cc >= 750


def decide(f):
    """The answers, as option letters, for questionnaire.run."""
    a = {}
    a["q0"] = TYPE_LETTER.get(f["bike_type"]) or ("A" if f["pure_offroad"] else "G")

    has_batt = battery(f)
    a["q1"] = "A" if has_batt else "B"
    if has_batt:
        a["q2"] = "A" if electric_start(f) else "B"
    a["q2b"] = "A" if turn_signals(f) else "B"
    if headlight(f):
        a["q2c"] = "A"
        if two_headlights(f):
            a["q2ca"], a["q2cb"] = "B", "A"
        else:
            a["q2ca"] = "A"
        a["q2cc"] = "A" if parking_light(f) else "B"
    else:
        a["q2c"] = "B"

    a["q3"] = drive(f)

    cyl = f["cyl"]
    a["q4"] = {1: "A", 2: "B", 3: "C", 4: "D", 5: "E", 6: "F"}.get(cyl, "G")
    if cyl in (2, 4, 6):
        a["q4a"] = f["arr"] or "B"

    if f["stroke"] == 2:
        a["q5"] = two_stroke_lube(f)
    else:
        a["q5"] = valvetrain(f)
        a["q6"] = oil_layout(f)

    if fuel_injected(f):
        a["q7"] = "A"
        a["q9"] = "A"                      # injection means an electric pump; q9a is skipped
    else:
        n = cyl or 1
        if f["make"] in ("Harley-Davidson", "Buell", "Indian", "Victory") or f["scooter"] or f["moped"]:
            n = 1
        elif f["meriden"] and cyl == 2 and has(f["model"], "tr6", "tr7", "tiger", "trophy", "thunderbird"):
            n = 1
        a["q7"] = {1: "B", 2: "C", 3: "D", 4: "E", 5: "F", 6: "G"}[min(n, 6)]
        a["q8"] = "A" if electronic_ignition(f) else "B"
        pump = fuel_pump(f)
        a["q9"] = "A" if pump else "B"
        if pump:
            a["q9a"] = pump

    a["q10"] = "A" if air_front(f) else "B"
    a["q11"] = "A" if air_rear(f) else "B"
    a["q12"] = "A" if hydraulic_clutch(f) else "B"
    a["q13"] = oil_filter(f)
    fd, rd = front_disc(f), rear_disc(f)
    a["q14"] = "A" if fd else "B"
    a["q15"] = "A" if rd else "B"
    t = tubes(f)
    a["q16"] = a["q17"] = "A" if t else "B"
    a["q18"] = f["cool"]
    a["q19"] = "A" if radiator_fan(f) else "B"
    if not fd:
        a["q20"] = "D"
    elif dual_front_discs(f):
        a["q20"], a["q20a"] = "C", "A"
    else:
        a["q20"] = "A"
    a["q21"] = "A" if rd else "B"
    return a


# ---------------------------------------------------------------------------
# Honda catalog bikes: what the model code says
# ---------------------------------------------------------------------------
def honda_features(code, y0, y1, cc, drive_type, bike_type):
    c = code.upper()
    m = c.replace("/", " ")
    stroke, cyl, arr, cool, cat, engine = 4, 1, None, "A", "standard", ""
    two_strokes = ("MB5", "MT125", "MT250", "NS-50", "NH80", "NQ-50", "NB-50", "NN-50", "SA-50",
                   "SE-50", "TG-50", "NU50", "NA-50", "NC-50", "PA-50", "SB-50", "NX-50")
    if any(m.startswith(p) for p in two_strokes):
        stroke = 2
    if m.startswith(("CH", "CN", "NH", "NQ", "NB", "NN", "SA", "SE", "SB", "TG", "NU", "NX-50")):
        cat = "scooter"
    elif m.startswith(("NA-50", "NC-50", "PA-50")):
        cat = "moped"
    elif m.startswith(("CT", "ZB", "Z50", "GROM", "MB5", "NS-50", "ST90", "SL70", "XL70", "XL80", "XR80", "TR200")):
        cat = "mini" if cc and cc < 100 else "trail"
    elif m.startswith(("XL", "XR", "NX", "SL", "TLR", "MT", "CRF")):
        cat = "dual-sport" if m.startswith(("XL", "NX", "SL", "CRF", "XR650L", "XR250L")) else "off-road"
        if m.startswith("TLR"):
            cat = "trials"
        if m.startswith("MT"):
            cat = "enduro"
    elif m.startswith(("VT", "VTX", "CM", "CMX", "CB750C", "CB900C", "CB650C", "CB1000C", "GL1500C", "VF1100C", "VF700C", "VF750C", "VF500C")):
        cat = "cruiser"
    elif m.startswith(("GL", "ST", "PC", "NT700", "CTX", "GL500", "GL650")):
        cat = "touring"
    elif m.startswith(("CBR", "VFR", "VF", "RC51", "VTR", "CB600F HURRICANE", "NT650")):
        cat = "sport"
    elif m.startswith("CBX"):
        cat = "sport"
    if m.startswith("GL") and "SILVER" not in m:
        cat = "touring"

    fours = ("CB350F", "CB400F", "CB500K", "CB550", "CB650", "CB700", "CB750", "CB900", "CB1000",
             "CB1100", "CB600", "CBR", "CB40F", "VF", "VFR", "ST1", "CTX1300", "CB400A", "RC30", "GL1000", "GL1100", "GL1200")
    twins = ("CB175", "CB250", "CB350", "CB360", "CB400T", "CB450", "CB500T", "CB500F", "CB500X",
             "CJ360", "CL175", "CL350", "CL360", "CL450", "CM", "CMX", "CX", "GL500", "GL650", "VT", "VTR",
             "NT", "PC800", "XL600V", "CTX700", "NC700", "CRF1000", "SL350")
    if m.startswith("CBX") or m.startswith(("GL1500", "GL1800")):
        cyl = 6
    elif m.startswith(fours) and not m.startswith(("CB350G", "CB350K", "CB450SC", "CB450T", "CB650SC", "CB750 NIGHTHAWK", "CB250")):
        cyl = 4
    elif m.startswith(twins):
        cyl = 2
    if m.startswith(("CB650SC", "CB750 NIGHTHAWK", "CB700SC")):
        cyl = 4
    if m.startswith("CB250 NIGHTHAWK"):
        cyl = 2
    if m.startswith(("CBR250", "CBR300", "CB300F")):
        cyl = 1
    if m.startswith(("CBR500", "CB500F", "CB500X")):
        cyl = 2
    if m.startswith(("CBR650", "CB600F", "CB1000R", "CB1000 ", "CB900F2")):
        cyl = 4

    if cyl in (2, 4, 6):
        if m.startswith(("CX", "GL")):
            arr = "C"
        elif m.startswith(("VT", "VF", "VTR", "RC51", "NT", "PC800", "XL600V", "ST1", "CTX1300")):
            arr = "A"
        else:
            arr = "B"

    liquid = ("CBR", "VF", "VT", "VTX", "VTR", "RC51", "CX", "GL", "ST1", "PC800", "NT", "CTX", "NC700",
              "CRF1000", "CB1000R", "CB600F", "CB500F", "CB500X", "CB300F", "CB1000 ", "CB900F2", "CH125", "CH150",
              "CH250", "CN250", "XL600V", "NX250", "GL500", "GL650", "CB40F", "CBR250", "CBR300")
    if m.startswith(liquid) and not m.startswith(("CB750", "VT500FT")):
        cool = "C"
    if m.startswith(("VT500", "VT700", "VT750", "VT800", "VT1100", "VT600", "VT1300")):
        cool = "C"

    dohc = m.startswith(("CBR", "VF", "VFR", "RC", "VTR", "CB1000R", "CB600F", "CB900F2", "CB500F", "CB500X",
                         "CB300F", "CB1000 ", "CB750 NIGHTHAWK", "CB700SC", "CB650SC", "CB450SC", "CB450",
                         "CB1100", "CB900F", "CB750F", "CB650", "CB550", "CB500K", "CB400F", "CB350F", "CBX",
                         "CTX1300", "ST1", "NC700", "CTX700", "CRF1000", "XL250R", "XL350R", "XL500", "XL600",
                         "XR", "NX", "FT500", "GB500", "CB400T", "CB450T", "CM400", "CM450", "CB250 NIGHTHAWK"))
    engine = ("dohc" if dohc else "sohc") + (" liquid-cooled" if cool == "C" else " air-cooled")
    if m.startswith(("CB750K", "CB750A", "CB750C", "CB750L", "CB550", "CB500K", "CB400F", "CB350F", "CB650")) and "NIGHTHAWK" not in m:
        engine = "sohc air-cooled"        # the SOHC fours
    shaft = drive_type == "Driveshaft" or m.startswith(("GL", "ST1", "CX", "PC800", "VT1100", "VT700", "VT750C ACE", "VT750C AERO", "VT1300", "VTX", "VF1100", "VF700C", "VF750C MAGNA", "VF700S", "VF1100S", "CB650C", "CB750C", "CB900C", "CB1000C", "CTX1300", "VFR1200", "NT700"))
    if drive_type == "Chain and sprockets":
        shaft = False
    engine += ", shaft" if shaft else ""
    if m.startswith(("CB750A", "CB400A", "CM400A", "CM450A")):
        engine += " automatic"

    model = code.title()
    f = base_features("Honda", model, y0, y1, cc, engine, stroke, cat, "", bike_type)
    parse_engine(f)
    f["cyl"], f["arr"], f["cool"] = cyl, arr, cool
    if m.startswith(("VF", "CBR", "VFR", "CB600F", "CB1000", "RC51", "VTR1000")):
        f["sport"] = True
    if m.startswith(("CB650SC", "CB700SC")):
        f["valves"] = "E"                  # Nighthawk hydraulic lifters
    return f


# ---------------------------------------------------------------------------
def key_for(label):
    return re.sub(r"[^a-z0-9]+", "_",
                  label.lower().replace("×", " x ").replace("&", " and ")).strip("_")


def main(argv):
    dry = "--dry-run" in argv
    only = None
    if "--make" in argv:
        only = argv[argv.index("--make") + 1]
    paths = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--make")]

    if not dry:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(f"{DB_PATH}.bak-{stamp}") as dst:
            src.backup(dst)      # the backup API sees the WAL; a file copy would not
        print(f"backup: data.db.bak-{stamp}")

    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")

    known = {r[0] for r in conn.execute("SELECT field_key FROM spec_fields")}
    triggers = defaultdict(list)
    for r in conn.execute("SELECT question_id, option_label, field_key FROM field_triggers"):
        triggers[(r[0], r[1])].append(r[2])

    # Workbook rows, keyed the way the import keyed them.
    rows_by_key = defaultdict(list)
    for p in paths:
        rows = read_rows(p)
        resolve_codes(rows)
        for r in rows:
            rows_by_key[(r["make"], r["model_code"].lower())].append(r)

    bikes = [dict(r) for r in conn.execute(
        "SELECT b.*, (SELECT value FROM specs s WHERE s.bike_id=b.id AND s.field_key='engine_displacement') AS disp,"
        "       (SELECT value FROM specs s WHERE s.bike_id=b.id AND s.field_key='drive_type') AS drive_type"
        " FROM bikes b WHERE NOT EXISTS (SELECT 1 FROM bike_answers a WHERE a.bike_id=b.id)"
        + (" AND b.make=?" if only else "") + " ORDER BY b.id", (only,) if only else ())]

    stats = Counter()
    dist = defaultdict(Counter)
    skipped, unmatched, samples, unsure_list = [], [], [], []
    for b in bikes:
        cc = None
        if b["disp"]:
            mm = re.match(r"(\d+)", b["disp"])
            cc = int(mm.group(1)) if mm else None
        # A workbook row is the better source wherever there is one. Honda's
        # catalogue bikes have no workbook, so they fall back to what their
        # model code says; a Honda that came from a workbook (the CR line)
        # is read from that workbook like every other make.
        cands = rows_by_key.get((b["make"], b["model_code"].lower()), [])
        row = next((r for r in cands if r["y0"] == b["year_start"]), None) or \
            next((r for r in cands if r["y0"] <= b["year_start"] <= (r["y1"] or THIS_YEAR)), None)
        if not row and b["make"] == "Honda":
            f = honda_features(b["model_code"], b["year_start"], b["year_end"], cc, b["drive_type"], b["bike_type"])
        else:
            if not row:
                unmatched.append(f"#{b['id']} {b['make']} {b['model_code']} {b['year_start']}")
                continue
            st = row["stroke"].lower()
            stroke = 2 if st.startswith("2") else 4 if st.startswith("4") else                 None if ("electric" in st or "electric" in row["engine"].lower()) else 4
            f = base_features(b["make"], strip_years(row["model"]), b["year_start"], b["year_end"], cc,
                              row["engine"], stroke, row["category"], row["notes"], b["bike_type"])
            parse_engine(f)
        if f["stroke"] is None or "electric" in f["engine"]:
            skipped.append(f"#{b['id']} {b['make']} {f['model']} (electric)")
            continue

        answers = decide(f)
        try:
            path, fields, not_sure, bike_type, effective = questionnaire.run(answers)
        except questionnaire.QuestionnaireError as e:
            skipped.append(f"#{b['id']} {b['make']} {f['model']}: {e}")
            continue

        created = 0
        for label in fields:
            key = key_for(label)
            if key not in known:
                raise SystemExit(f"questionnaire triggered unregistered field {label!r}")
            created += conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence) VALUES (?,?,NULL,'pending')",
                (b["id"], key)).rowcount
        for qid, opt in effective.items():
            for key in triggers.get((qid, opt), []):
                created += conn.execute(
                    "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence) VALUES (?,?,NULL,'pending')",
                    (b["id"], key)).rowcount
        created += conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
            " SELECT ?, field_key, NULL, 'pending' FROM spec_fields WHERE universal=1", (b["id"],)).rowcount
        conn.executemany(
            "INSERT INTO bike_answers (bike_id, question_id, option_label, answered_by) VALUES (?,?,?,NULL)",
            [(b["id"], qid, opt) for qid, opt in effective.items()])
        for item in not_sure:
            conn.execute("INSERT INTO not_sure_answers (bike_id, question_text, submitted_by) VALUES (?,?,NULL)",
                         (b["id"], item["text"]))
        if bike_type and not b["bike_type"]:
            conn.execute("UPDATE bikes SET bike_type=? WHERE id=?", (bike_type, b["id"]))

        stats["bikes"] += 1
        stats["specs"] += created
        stats["not_sure"] += len(not_sure)
        for item in not_sure:
            unsure_list.append(f"#{b['id']} {b['make']} {f['model']}: {item['question_id']} {item['text'][:50]}")
        for qid, opt in effective.items():
            dist[qid][opt] += 1
        if len(samples) < 400 and (stats["bikes"] % 9 == 0):
            samples.append((b["id"], b["make"], f["model"], f["y0"], f["y1"], f["cc"], effective))

    print(f"{'DRY RUN -- nothing written' if dry else 'written'}: {stats['bikes']} bikes answered,"
          f" {stats['specs']} spec rows created, {stats['not_sure']} not-sure answers")
    if unmatched:
        print(f"\n{len(unmatched)} bikes had no workbook row (not answered):")
        for s in unmatched:
            print("  " + s)
    if skipped:
        print(f"\n{len(skipped)} bikes skipped:")
        for s in skipped:
            print("  " + s)
    if unsure_list:
        print(f"\n{len(unsure_list)} not-sure answers (each goes to the admin queue):")
        for s in unsure_list:
            print("  " + s)

    doc = questionnaire.load()
    print("\nanswer distribution:")
    for qid in doc["primary_order"] + [q for q in doc["questions"] if q not in doc["primary_order"]]:
        if qid not in dist:
            continue
        q = doc["questions"][qid]
        opts = {o["l"]: o["t"] for o in q["options"]}
        parts = ", ".join(f"{opts[l][:28]}={n}" for l, n in dist[qid].most_common())
        print(f"  {qid:4s} {parts}")

    if "--samples" in argv:
        print("\nsamples:")
        qs = doc["questions"]
        for bid, make, model, y0, y1, cc, eff in samples:
            words = "; ".join(f"{qid}={next(o['t'] for o in qs[qid]['options'] if o['l'] == opt)[:22]}"
                              for qid, opt in eff.items() if qid != "q0")
            print(f"  #{bid} {make} {model} {y0}-{y1 or 'present'} {cc}cc\n      {words}")

    if dry:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
