"""Import the per-make model-list workbooks into data.db.

    py import_model_lists.py [--dry-run] <workbook.xlsx> [...]

One workbook row becomes one bike: a row is already "one machine" by the rule
the lists were built on (split when the engine or frame changed, year-scope
when only a part did), so it maps onto a bike the way a split does. What the
row carries goes where the schema already keeps it:

  Make, Model code(s)           -> bikes.make / bikes.model_code
  Model                          -> the primary name, plus one extra bike_names
                                    row per "A / B / C" variant so search finds
                                    "XS650" on the "XS-1 / XS-2 / TX650 / XS650"
                                    bike
  First / Last model year        -> bikes.year_start / year_end ("present" = NULL,
                                    still current) and one bike_years row per year
  Engine size (cc), Engine       -> engine_displacement / cylinder_configuration
                                    specs, confidence 'pending': they come from
                                    the research behind the list, not a manual
  Category                       -> bikes.bike_type (the questionnaire's q0 list)

Universal fields go on every bike exactly as POST /api/bikes does it. Nothing
else is guessed: Stroke, Markets, Notes and Confidence have no column in the
schema and are left in the workbook. years_verified stays 0 -- the list is a
catalogue, and a manager still has to confirm the span.

A row is never merged into a bike already in the database. Where a bike of
the same make and model code already covers some of its years (the XT500,
the FXR entered by hand), that bike stays as it is and only the years around
it go in, as bikes of their own. Every such case is reported. A dry run
(--dry-run) does all of this inside a transaction and rolls it back.

An optional "Same as (bike ids)" column names bikes already in the database
that ARE this row's machine under another code -- the Honda seed's
"GL1500C/CT VALKYRIE/VALKYRIE TOUR" against a row called "Valkyrie". Those
count as covering the row's years exactly like a code match. When the row
names one such bike and the row is one machine (not a family), that bike's
year span is widened to the row's rather than fragments being added around
it; its bike_type is set from the row's category where the seed left the
default; and the row's other names are added so search finds them.
"""
import datetime
import os
import re
import shutil
import sqlite3
import sys

import openpyxl

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data.db")
THIS_YEAR = datetime.date.today().year

# Category text in the workbooks is free-form ("Sport-touring", "Cruiser /
# Muscle", "Dual-sport / Supermoto"); q0's options are fixed. First match wins,
# so the order matters: "Sport-touring" is Touring, not Sport.
TYPE_RULES = [
    (("scooter", "underbone", "moped"), "Scooter"),
    (("motocross", "enduro", "off-road", "trials", "trail", "mini", "cross-country",
      "minicross", "flat track", "speedway"), "Dirt bike / Off-road"),
    (("dual-sport", "adventure", "supermoto", "rally"), "Dual-sport / Adventure"),
    (("cruiser", "bobber", "chopper", "bagger"), "Cruiser"),
    (("touring",), "Touring"),
    (("naked", "retro", "standard", "street", "classic", "commuter", "roadster",
      "cafe", "café", "scrambler", "modern classic"), "Standard / Naked"),
    (("sport", "superbike", "supersport", "racer"), "Street bike / Sport bike"),
]


def bike_type_for(make, category):
    if make == "Harley-Davidson":
        return "Harley-Davidson"
    text = (category or "").lower()
    for words, kind in TYPE_RULES:
        if any(w in text for w in words):
            return kind
    return None


# "YZF-R6 (1999-2020 road; race-only 2021-)" -> "YZF-R6". Only a parenthetical
# that carries years or a decade comes off; "(India commuters)" is part of the
# name and stays.
YEAR_PAREN = re.compile(r"\s*\(([^()]*(?:\d{4}|present|\d0s)[^()]*)\)\s*$", re.I)


def strip_years(model):
    return YEAR_PAREN.sub("", model).strip()


def variants(stripped):
    """The individual names inside "XS-1 / XS-2 / TX650 / XS650 / XS650 Special"."""
    if " / " not in stripped:
        return []
    out = []
    for part in stripped.split(" / "):
        part = part.strip()
        if len(part) >= 2 and part != stripped and part not in out:
            out.append(part)
    return out


def is_family(stripped):
    """Several displacements named in one row ("XJ550 / XJ650 / XJ900") means
    the cc column is the largest of them, and the spec has to say so."""
    nums = {int(n) for n in re.findall(r"\d+", stripped) if int(n) >= 50}
    return len(nums) >= 2


def cell_int(v):
    if v is None or v == "":
        return None
    if isinstance(v, str):
        v = v.strip()
        if not v or not re.match(r"^-?\d+(\.\d+)?$", v):
            return None
    return int(round(float(v)))


def read_rows(path):
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    header = [str(h).strip() if h is not None else "" for h in next(it)]
    col = {h: i for i, h in enumerate(header)}

    def get(row, *names):
        for n in names:
            if n in col and col[n] < len(row):
                return row[col[n]]
        return None

    out = []
    for r in it:
        model = get(r, "Model")
        if not model or not str(model).strip():
            continue
        make = str(get(r, "Make") or "").strip()
        y0 = cell_int(get(r, "First model year"))
        last = get(r, "Last model year")
        y1 = None if (isinstance(last, str) and last.strip().lower() == "present") else cell_int(last)
        if y0 is None:
            raise SystemExit(f"{os.path.basename(path)}: no first year on {model!r}")
        if y1 is not None and y1 < y0:
            raise SystemExit(f"{os.path.basename(path)}: last year before first on {model!r}")

        # The Harley workbook carries launch and latest displacement; the
        # others one figure. The launch figure sorts the model filter (first
        # number in the value) and the latest one is kept in the text.
        cc = cell_int(get(r, "Engine size (cc)"))
        cc_launch = cell_int(get(r, "Engine size at launch (cc)"))
        cc_latest = cell_int(get(r, "Engine size, latest (cc)"))

        out.append({
            "make": make,
            "model": str(model).strip(),
            "code": str(get(r, "Model code(s)") or "").strip(),
            "y0": y0, "y1": y1,
            "cc": cc, "cc_launch": cc_launch, "cc_latest": cc_latest,
            "engine": str(get(r, "Engine") or "").strip(),
            "category": str(get(r, "Category") or "").strip(),
            "stroke": str(get(r, "Stroke") or "").strip(),
            "notes": str(get(r, "Notes") or "").strip(),
            "confidence": str(get(r, "Confidence") or "").strip(),
            "same": [int(x) for x in re.findall(r"\d+", str(get(r, "Same as (bike ids)") or ""))],
        })
    return out


def displacement_value(row, stripped):
    if row["cc_launch"] is not None or row["cc_latest"] is not None:
        a, b = row["cc_launch"], row["cc_latest"]
        if a is None:
            return f"{b}cc"
        if b is None or b == a:
            return f"{a}cc"
        return f"{a}cc at launch, later {b}cc"
    if row["cc"] is None:
        return None
    if is_family(stripped):
        return f"{row['cc']}cc (largest in the family)"
    return f"{row['cc']}cc"


def overlaps(a0, a1, b0, b1):
    a1 = THIS_YEAR if a1 is None else a1
    b1 = THIS_YEAR if b1 is None else b1
    return a0 <= b1 and b0 <= a1


def remaining_spans(y0, y1, taken):
    """The years of y0-y1 not covered by the bikes in `taken`, as contiguous
    spans. An open end stays open."""
    end = THIS_YEAR if y1 is None else y1
    covered = set()
    for e in taken:
        e_end = THIS_YEAR if e["year_end"] is None else e["year_end"]
        covered.update(range(e["year_start"], e_end + 1))
    spans, run = [], []
    for y in range(y0, end + 1):
        if y in covered:
            if run:
                spans.append(run); run = []
        else:
            run.append(y)
    if run:
        spans.append(run)
    return [(sp[0], None if (y1 is None and sp[-1] == end) else sp[-1]) for sp in spans]


def resolve_codes(rows):
    """Two rows of one make may share a code ("XT" for two eras) as long as
    their years do not overlap -- that is the same rule a split follows. Where
    they do overlap, the row's full model name becomes its code."""
    for r in rows:
        r["model_code"] = r["code"] or strip_years(r["model"])
    by_key = {}
    for r in rows:
        by_key.setdefault((r["make"], r["model_code"]), []).append(r)
    renamed = []
    for key, group in by_key.items():
        if len(group) < 2:
            continue
        clash = False
        for i, a in enumerate(group):
            for b in group[i + 1:]:
                if overlaps(a["y0"], a["y1"], b["y0"], b["y1"]):
                    clash = True
        if clash:
            for r in group:
                r["model_code"] = strip_years(r["model"])
                renamed.append(r)
    # The full name can still collide (two eras of one long name at the same
    # start year); pin those with the start year.
    seen = {}
    for r in rows:
        k = (r["make"], r["model_code"], r["y0"])
        if k in seen:
            r["model_code"] = f"{r['model_code']} ({r['y0']})"
            renamed.append(r)
        seen[k] = r
    return renamed


def widen_bike(conn, e, r, stripped, names, totals):
    """Stretch a hand-entered bike over the row's whole span. Refused (False)
    when another bike of the model already owns one of the new years -- the
    bike_years overlap trigger says so -- and the row then goes in the usual
    way, around the bike."""
    # Widen only, never narrow: the union of the two spans. A seed year the
    # row does not claim stays -- the row is a catalogue, not a correction.
    y0 = min(e["year_start"], r["y0"])
    y1 = None if (e["year_end"] is None or r["y1"] is None) else max(e["year_end"], r["y1"])
    r["y0"], r["y1"] = y0, y1
    conn.execute("SAVEPOINT widen")
    try:
        conn.execute("UPDATE bikes SET year_start=?, year_end=?, years_verified=0 WHERE id=?",
                     (y0, y1, e["id"]))
        last = y1 if y1 is not None else THIS_YEAR
        for y in range(y0, last + 1):
            cur = conn.execute(
                "INSERT OR IGNORE INTO bike_years (bike_id, year, market) VALUES (?,?,'')",
                (e["id"], y))
            totals["years"] += cur.rowcount
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK TO widen")
        conn.execute("RELEASE widen")
        return False
    conn.execute("RELEASE widen")
    touch_seed_bike(conn, e, r, stripped, names, totals)
    return True


def touch_seed_bike(conn, e, r, stripped, names, totals):
    """What a linked row can safely add to a bike that was already there: the
    kind of bike where the seed left the default, the row's other names, and
    the row's displacement / engine where the bike has none."""
    kind = bike_type_for(r["make"], r["category"])
    cur_type = conn.execute("SELECT bike_type FROM bikes WHERE id=?", (e["id"],)).fetchone()[0]
    if kind and cur_type in (None, "Standard / Naked") and kind != cur_type:
        conn.execute("UPDATE bikes SET bike_type=? WHERE id=?", (kind, e["id"]))
    for n in names + [stripped]:
        full = f"{r['make']} {n}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO bike_names (bike_id, name, market, is_primary) VALUES (?,?,'',0)",
            (e["id"], full))
        totals["names"] += cur.rowcount
    for key, value in (("engine_displacement", displacement_value(r, stripped)),
                       ("cylinder_configuration", r["engine"] or None)):
        if value and not conn.execute(
                "SELECT 1 FROM specs WHERE bike_id=? AND field_key=? AND value IS NOT NULL",
                (e["id"], key)).fetchone():
            conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence) VALUES (?,?,?,'pending')",
                (e["id"], key, value))
            conn.execute("UPDATE specs SET value=?, confidence='pending' WHERE bike_id=? AND field_key=?"
                         " AND value IS NULL", (value, e["id"], key))


def main(argv):
    dry = "--dry-run" in argv
    paths = [a for a in argv if not a.startswith("--")]
    if not paths:
        raise SystemExit(__doc__)
    if not os.path.exists(DB_PATH):
        raise SystemExit("data.db not found")

    if not dry:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = f"{DB_PATH}.bak-{stamp}"
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(backup) as dst:
            src.backup(dst)      # the backup API sees the WAL; a file copy would not
        print(f"backup: {os.path.basename(backup)}")

    # Autocommit off at the driver level and one explicit transaction: the
    # driver's implicit BEGIN only fires for INSERT/UPDATE/DELETE, not for
    # SAVEPOINT, so without this each RELEASE would commit its row and a dry
    # run would write.
    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")

    universal = [r[0] for r in conn.execute(
        "SELECT field_key FROM spec_fields WHERE universal=1")]
    have_field = {r[0] for r in conn.execute("SELECT field_key FROM spec_fields")}
    for k in ("engine_displacement", "cylinder_configuration"):
        if k not in have_field:
            raise SystemExit(f"spec field {k} missing -- run seed/migrations first")

    totals = {"bikes": 0, "names": 0, "years": 0, "specs": 0, "skipped": 0}
    skipped, near, within, renamed_all = [], [], [], []

    for path in paths:
        rows = read_rows(path)
        renamed_all += resolve_codes(rows)
        make = rows[0]["make"] if rows else "?"
        existing = [dict(r) for r in conn.execute(
            "SELECT b.id, b.model_code, b.year_start, b.year_end,"
            " (SELECT GROUP_CONCAT(n.name, '|') FROM bike_names n WHERE n.bike_id=b.id) AS names"
            " FROM bikes b WHERE b.make=?", (make,))]
        before = {e["id"] for e in existing}
        added = 0

        queue = list(rows)
        while queue:
            r = queue.pop(0)
            stripped = strip_years(r["model"])
            names = variants(stripped)
            primary = f"{r['make']} {stripped}"

            # Same code, overlapping years: that part of the row is already
            # in. A hand-entered bike is one machine by the same rule the
            # list follows, so the years around it -- the FXR before and
            # after the 1984-86 one already on the site -- go in as bikes of
            # their own rather than being dropped with it.
            dup = [e for e in existing
                   if e["model_code"].lower() == r["model_code"].lower()
                   and overlaps(r["y0"], r["y1"], e["year_start"], e["year_end"])]
            linked = [e for e in existing if e["id"] in r.get("same", [])
                      and overlaps(r["y0"], r["y1"], e["year_start"], e["year_end"])]
            for e in linked:
                if e not in dup:
                    dup.append(e)
            if len(linked) == 1 and len(dup) == 1 and not is_family(stripped):
                # One bike, one machine: widen its span to the row's rather
                # than leave slivers of years as bikes of their own.
                e = linked[0]
                widened = widen_bike(conn, e, r, stripped, names, totals)
                if widened:
                    e["year_start"], e["year_end"] = r["y0"], r["y1"]
                    skipped.append(f"{primary} {r['y0']}-{r['y1'] or 'present'}"
                                   f"  (bike #{e['id']} {e['model_code']} widened to it)")
                    totals["skipped"] += 1
                    continue
            for e in existing:
                if e["id"] in r.get("same", []):
                    touch_seed_bike(conn, e, r, stripped, names, totals)
            if dup:
                e = dup[0]
                rest = remaining_spans(r["y0"], r["y1"], dup)
                skipped.append(f"{primary} {r['y0']}-{r['y1'] or 'present'}"
                               f"  (bike #{e['id']} {e['model_code']}"
                               f" {e['year_start']}-{e['year_end'] or 'present'} already covers it"
                               + (";  the rest goes in as "
                                  + ", ".join(f"{a}-{b or 'present'}" for a, b in rest)
                                  if rest else "") + ")")
                totals["skipped"] += 1
                for a, b in rest:
                    queue.insert(0, dict(r, y0=a, y1=b, same=[]))
                continue

            # A hand-entered bike whose code is one of this row's variant
            # names is the same machine under a narrower name. It goes in
            # anyway -- the row covers more than that one bike -- but it is
            # reported so the two can be reconciled by hand.
            # The same overlap between two rows of one list is by design (a
            # KX250 two-stroke and a KX250F are different machines) and is
            # only listed for a look.
            lowered = {n.lower() for n in names} | {stripped.lower()}
            for e in existing:
                if (e["model_code"].lower() in lowered
                        and overlaps(r["y0"], r["y1"], e["year_start"], e["year_end"])):
                    line = (f"{primary} {r['y0']}-{r['y1'] or 'present'}"
                            f"  overlaps bike #{e['id']} {r['make']} {e['model_code']}"
                            f" {e['year_start']}-{e['year_end'] or 'present'}")
                    (near if e["id"] in before else within).append(line)

            conn.execute("SAVEPOINT row")
            try:
                cur = conn.execute(
                    "INSERT INTO bikes (make, model_code, year_start, year_end,"
                    " bike_type, years_verified) VALUES (?,?,?,?,?,0)",
                    (r["make"], r["model_code"], r["y0"], r["y1"],
                     bike_type_for(r["make"], r["category"])))
                bike_id = cur.lastrowid

                conn.execute(
                    "INSERT INTO bike_names (bike_id, name, market, is_primary)"
                    " VALUES (?,?,'',1)", (bike_id, primary))
                totals["names"] += 1
                for n in names:
                    full = f"{r['make']} {n}"
                    if full == primary:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO bike_names (bike_id, name, market, is_primary)"
                        " VALUES (?,?,'',0)", (bike_id, full))
                    totals["names"] += 1

                last = r["y1"] if r["y1"] is not None else THIS_YEAR
                for y in range(r["y0"], last + 1):
                    conn.execute(
                        "INSERT INTO bike_years (bike_id, year, market) VALUES (?,?,'')",
                        (bike_id, y))
                    totals["years"] += 1

                for key in universal:
                    conn.execute(
                        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                        " VALUES (?,?,NULL,'pending')", (bike_id, key))
                    totals["specs"] += 1
                disp = displacement_value(r, stripped)
                if disp:
                    conn.execute(
                        "INSERT INTO specs (bike_id, field_key, value, confidence)"
                        " VALUES (?,?,?,'pending')", (bike_id, "engine_displacement", disp))
                    totals["specs"] += 1
                if r["engine"]:
                    conn.execute(
                        "INSERT INTO specs (bike_id, field_key, value, confidence)"
                        " VALUES (?,?,?,'pending')",
                        (bike_id, "cylinder_configuration", r["engine"]))
                    totals["specs"] += 1
            except sqlite3.IntegrityError as e:
                conn.execute("ROLLBACK TO row")
                conn.execute("RELEASE row")
                skipped.append(f"{primary} {r['y0']}-{r['y1'] or 'present'}  ({e})")
                totals["skipped"] += 1
                continue
            conn.execute("RELEASE row")
            existing.append({"id": bike_id, "model_code": r["model_code"],
                             "year_start": r["y0"], "year_end": r["y1"], "names": primary})
            added += 1
            totals["bikes"] += 1

        print(f"{make:16s} {os.path.basename(path)}: {added} of {len(rows)} rows added")

    if renamed_all:
        print(f"\n{len(renamed_all)} rows used the full model name as the code"
              " (their short code overlapped another row):")
        for r in renamed_all:
            print(f"  {r['make']} {r['model_code']!r} {r['y0']}-{r['y1'] or 'present'}"
                  f"  (code column: {r['code']!r})")
    if skipped:
        print(f"\n{len(skipped)} rows skipped:")
        for s in skipped:
            print("  " + s)
    if near:
        print(f"\n{len(near)} rows added that overlap a bike already entered by hand:")
        for s in near:
            print("  " + s)
    if within:
        print(f"\n{len(within)} rows whose name overlaps another row of the same list"
              " (different machines by design -- worth a look):")
        for s in within:
            print("  " + s)

    print(f"\n{'DRY RUN -- nothing written' if dry else 'written'}: "
          f"{totals['bikes']} bikes, {totals['names']} names, {totals['years']} model years,"
          f" {totals['specs']} specs, {totals['skipped']} skipped")
    if dry:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
        print("bikes in database now:",
              conn.execute("SELECT COUNT(*) FROM bikes").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
