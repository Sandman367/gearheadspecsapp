"""
Read-only audit of the BIKES: where a machine's record contradicts itself.

The catalogue was imported from model-list workbooks. The import was honest
about what it did not know -- years_verified stays 0 on every row it made --
but the questionnaire answers it derived are guesses from a model code, and a
guess about whether an engine is a two-stroke puts a valve clearance spec on a
bike that has no valves.

This finds the cases where the record disagrees with ITSELF, which needs no
outside knowledge and cannot be argued with:

  A. IMPOSSIBLE YEARS      start after end, an end in the future, a span no
                           model ever had, bike_years outside the span.
  B. STROKE CONTRADICTION  a bike carrying both two-stroke-only and
                           four-stroke-only specs at once.
  C. ANSWER vs SPECS       what q5 says against what the bike actually holds.
  D. DUPLICATE MACHINES    same make and model code, overlapping years.
  E. NO ANSWERS AT ALL     a bike nobody has run through the questionnaire.
  F. WORTH A LOOK          model families that are two-stroke by name, marked
                           four-stroke here. Knowledge, not arithmetic, so it
                           is listed separately and never counted as proven.

Changes nothing.

Run:  py audit_bikes.py [path/to/data.db] [--csv out.csv]
"""
import collections
import csv
import datetime
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")
THIS_YEAR = datetime.date.today().year

# Specs only a two-stroke has, and specs only a four-stroke has. A bike
# holding one of each is wrong whichever way round it is.
TWO_ONLY = ("premix_fuel_oil_ratio", "2_stroke_oil_pre_mix", "auto_lube_oil_type",
            "auto_lube_2_stroke_oil", "auto_lube_oil_tank_capacity")
FOUR_ONLY = ("valve_clearance_intake", "valve_clearance_exhaust",
             "intake_shim_size", "exhaust_shim_size")

# Model families that are two-stroke by name, PER MAKE.
#
# This is the only check that can find the real fault, because the fault is
# invisible from inside the database: the importer read a Stroke column from a
# workbook that was never stored, and a blank cell silently became FOUR. So a
# mis-marked two-stroke looks perfectly consistent -- its answers and its
# specs were both derived from the same wrong premise.
#
# Scoped by make, because the letters are not unique: Suzuki's GT triples are
# two-strokes and Ducati's GT 1000 is not; Yamaha's CT is a two-stroke and
# Honda's CT110 Trail is a four-stroke. An unscoped pattern accuses the wrong
# bike, and a list that cries wolf gets somebody to "correct" a record that
# was right.
TWO_STROKE_NAMES = {
    "Suzuki": [(r"^RM\s?\d", "RM"), (r"^JR\s?\d", "JR"), (r"^RG\s?\d", "RG"),
               (r"^RGV", "RGV"), (r"^TS\s?\d", "TS"), (r"^TM\s?\d", "TM"),
               (r"^PE\s?\d", "PE"), (r"^X-?7", "X-7"), (r"^GT\s?\d{3}", "GT triple")],
    "Kawasaki": [(r"^KX\s?\d", "KX"), (r"^KDX", "KDX"), (r"^KH\s?\d", "KH"),
                 (r"^H[12]\b", "triple"), (r"^S[123]\b", "S-series"),
                 (r"^AR\s?\d", "AR"), (r"^KR-?1", "KR-1")],
    "Honda": [(r"^CR\s?\d", "CR"), (r"^NSR", "NSR"), (r"^MB\s?\d", "MB"),
              (r"^MT\s?\d", "MT"), (r"^MVX", "MVX"), (r"^NS\s?\d{3}", "NS")],
    "Yamaha": [(r"^YZ\s?\d", "YZ"), (r"^DT\s?\d", "DT"), (r"^RD\s?\d", "RD"),
               (r"^RZ\s?\d", "RZ"), (r"^IT\s?\d", "IT"), (r"^TZ", "TZ"),
               (r"^PW\s?\d", "PW"), (r"^YSR", "YSR"), (r"^CT\s?\d", "CT")],
    "Aprilia": [(r"^RS\s?125", "RS125"), (r"^RX\s?\d", "RX"), (r"^SR\s?50", "SR50")],
    "GasGas": [(r"^EC\s?\d", "EC"), (r"^TXT", "TXT")],
    "Husqvarna": [(r"^WR\s?\d{2,3}$", "WR"), (r"^CR\s?\d", "CR")],
    "Derbi": [(r"^Senda", "Senda"), (r"^GPR\s?50", "GPR50")],
}

# ...and these break the patterns above without being two-strokes: the
# four-stroke successor that kept the family letters, however it is spelled.
# The bare "<number>F" catches EC 450F, KX250F, YZ426F and WR250F in one.
NOT_TWO = re.compile(
    r"(four-?stroke|4-?stroke|4T\b"
    r"|\d\s?F\b|\d\s?FS?[ER]?\b"        # 250F, 450 FSE, 515 FSR
    r"|CRF|CRE\d|KXF|RM-?Z|RMX\s?450|YZF"
    r"|TE\s?\d{3}|FE\s?\d|FC\s?\d|SX-?F|EXC-?F|RS4)", re.I)


def rows(cur):
    return [dict(r) for r in cur]


def audit(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    found = []           # (severity, bike_id, name, check, detail)

    bikes = rows(conn.execute(
        "SELECT b.id, b.make, b.model_code, b.year_start, b.year_end,"
        "       b.bike_type, b.years_verified,"
        "       (SELECT name FROM bike_names n WHERE n.bike_id=b.id AND n.is_primary=1) AS name"
        " FROM bikes b ORDER BY b.make, b.model_code"))
    by_id = {b["id"]: b for b in bikes}

    def name_of(b):
        return b["name"] or f"{b['make']} {b['model_code']}"

    # ---- A. impossible years -------------------------------------------
    span = {}
    for r in conn.execute("SELECT bike_id, MIN(year) lo, MAX(year) hi, COUNT(*) n"
                          " FROM bike_years GROUP BY bike_id"):
        span[r["bike_id"]] = (r["lo"], r["hi"], r["n"])
    for b in bikes:
        s, e = b["year_start"], b["year_end"]
        if s and e and e < s:
            found.append(("wrong", b["id"], name_of(b), "years",
                          f"starts {s}, ends {e}"))
        if e and e > THIS_YEAR + 1:
            found.append(("wrong", b["id"], name_of(b), "years",
                          f"ends {e}, which is in the future"))
        if s and s < 1885:
            found.append(("wrong", b["id"], name_of(b), "years",
                          f"starts {s}, before motorcycles"))
        if s and e and e - s > 45:
            found.append(("check", b["id"], name_of(b), "years",
                          f"{s}-{e} is a {e - s} year span"))
        lo, hi, n = span.get(b["id"], (None, None, 0))
        if lo is not None and s and e and (lo < s or hi > e):
            found.append(("wrong", b["id"], name_of(b), "years",
                          f"listed {s}-{e} but has year rows {lo}-{hi}"))

    # ---- B. two-stroke and four-stroke at once --------------------------
    ph2 = ",".join("?" * len(TWO_ONLY))
    ph4 = ",".join("?" * len(FOUR_ONLY))
    two = {r[0] for r in conn.execute(
        f"SELECT DISTINCT bike_id FROM specs WHERE field_key IN ({ph2})", TWO_ONLY)}
    four = {r[0] for r in conn.execute(
        f"SELECT DISTINCT bike_id FROM specs WHERE field_key IN ({ph4})", FOUR_ONLY)}
    for bid in sorted(two & four):
        b = by_id.get(bid)
        if b:
            found.append(("wrong", bid, name_of(b), "stroke",
                          "carries both two-stroke and four-stroke specs"))

    # ---- C. what q5 says vs what the bike holds -------------------------
    q5 = {r["bike_id"]: r["option_label"] for r in conn.execute(
        "SELECT bike_id, option_label FROM bike_answers WHERE question_id='q5'")}
    for bid, ans in q5.items():
        b = by_id.get(bid)
        if not b:
            continue
        if ans in ("A", "B") and bid in four:
            found.append(("wrong", bid, name_of(b), "stroke",
                          f"answered q5={ans} (two-stroke) but carries valve clearance specs"))
        if ans in ("C", "D") and bid in two:
            found.append(("wrong", bid, name_of(b), "stroke",
                          f"answered q5={ans} (four-stroke) but carries two-stroke oil specs"))

    # ---- D. the same machine twice --------------------------------------
    seen = collections.defaultdict(list)
    for b in bikes:
        seen[(b["make"], (b["model_code"] or "").strip().lower())].append(b)
    for (make, code), group in sorted(seen.items()):
        if len(group) < 2 or not code:
            continue
        for i, x in enumerate(group):
            for y in group[i + 1:]:
                xs, xe = x["year_start"] or 0, x["year_end"] or THIS_YEAR
                ys, ye = y["year_start"] or 0, y["year_end"] or THIS_YEAR
                if xs <= ye and ys <= xe:
                    found.append(("check", x["id"], name_of(x), "duplicate",
                                  f"overlaps bike {y['id']} ({ys}-{ye})"))

    # ---- E. never answered ----------------------------------------------
    answered = {r[0] for r in conn.execute("SELECT DISTINCT bike_id FROM bike_answers")}
    for b in bikes:
        if b["id"] not in answered:
            found.append(("check", b["id"], name_of(b), "unanswered",
                          "no questionnaire answers at all"))

    # ---- F. two-stroke by name, four-stroke here ------------------------
    hints = []
    for b in bikes:
        code = (b["model_code"] or "").strip()
        label = b["name"] or ""
        if NOT_TWO.search(code) or NOT_TWO.search(label):
            continue
        for pat, family in TWO_STROKE_NAMES.get(b["make"], ()):
            if re.match(pat, code, re.I):
                if q5.get(b["id"]) in ("C", "D") or b["id"] in four:
                    hints.append((b["id"], name_of(b), f"{b['make']} {family}",
                                  f"q5={q5.get(b['id'])}"))
                break
    return found, hints, len(bikes)


def main():
    args = [a for a in sys.argv[1:]]
    out_csv = None
    if "--csv" in args:
        i = args.index("--csv")
        out_csv = args[i + 1]
        del args[i:i + 2]
    db = args[0] if args else DEFAULT_DB
    found, hints, total = audit(db)

    by_check = collections.Counter(f[3] for f in found)
    wrong = [f for f in found if f[0] == "wrong"]
    check = [f for f in found if f[0] == "check"]

    print(f"{total} bikes audited\n")
    print(f"CONTRADICTS ITSELF: {len(wrong)}")
    for c, n in collections.Counter(f[3] for f in wrong).most_common():
        print(f"    {c:12} {n}")
    print(f"\nWORTH A LOOK: {len(check)}")
    for c, n in collections.Counter(f[3] for f in check).most_common():
        print(f"    {c:12} {n}")

    print(f"\n--- the first 25 that contradict themselves ---")
    for sev, bid, name, chk, detail in wrong[:25]:
        print(f"  {bid:5} {name[:38]:38} {chk:10} {detail}")
    if len(wrong) > 25:
        print(f"  ... and {len(wrong) - 25} more")

    print(f"\n--- two-stroke by name, four-stroke in the record: {len(hints)} ---")
    for bid, name, family, why in hints[:20]:
        print(f"  {bid:5} {name[:38]:38} {family:14} {why}")
    if len(hints) > 20:
        print(f"  ... and {len(hints) - 20} more")

    if out_csv:
        with open(out_csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["severity", "bike_id", "bike", "check", "detail"])
            for row in found:
                w.writerow(row)
            for bid, name, family, why in hints:
                w.writerow(["check", bid, name, "two-stroke?",
                            f"{family} is a two-stroke family, {why}"])
        print(f"\nwritten to {out_csv}")


if __name__ == "__main__":
    main()
