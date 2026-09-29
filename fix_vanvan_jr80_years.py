"""
Correct the years of the Suzuki JR80 and the VanVans, and untangle the two
RV125 records. See BIKE_ERRORS.md items 1-3.

  JR80           2018-2023  ->  2001-present   (K1 = 2001; still sold, e.g. Suzuki NZ)
  RV200 VanVan   1972-2019  ->  2002-2020      (K2 = 2002 to M0 = 2020; no 1970s RV200)
  RV125 VanVan   1972-2019  ->  2003-2016      the four-stroke revival only; renamed
                                               from "RV125 VanVan (1972-82)"
  RV125          1976-1981  ->  1972-1982      the original two-stroke, and marked
                                               as one (q5 = A, auto-lube)

Sources: suzukicycles.org model histories (RV125, RV200, JR80), Wikipedia
"Suzuki RV125", autoevolution. The RV125 record already held the original
run, so the VanVan record is narrowed to the revival rather than split --
splitting would have made a second copy of the 1970s bike.

Model years outside a bike's new span are removed only if nobody has that
year in their garage. A catalogue "cylinder configuration" value that
described both engines is corrected to the one that bike has; any other
value is left alone.

Runs once: it acts only while the RV200 still starts in 1972.

Run:  py fix_vanvan_jr80_years.py [path/to/data.db]
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import questionnaire  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data.db")
THIS_YEAR = 2026
MIXED = "Single (two-stroke to 1982, four-stroke from 2003)"


def _bike(conn, model_code):
    row = conn.execute("SELECT id FROM bikes WHERE make='Suzuki' AND model_code=?", (model_code,)).fetchone()
    return row[0] if row else None


def _set_years(conn, bike_id, start, end, verified):
    last = end if end is not None else THIS_YEAR
    conn.execute("UPDATE bikes SET year_start=?, year_end=?, years_verified=? WHERE id=?",
                 (start, end, verified, bike_id))
    conn.execute(
        "DELETE FROM bike_years WHERE bike_id=? AND (year<? OR year>?)"
        " AND id NOT IN (SELECT bike_year_id FROM user_bikes WHERE bike_year_id IS NOT NULL)",
        (bike_id, start, last))
    for y in range(start, last + 1):
        conn.execute("INSERT OR IGNORE INTO bike_years (bike_id, year, market) VALUES (?,?,'')", (bike_id, y))


def _fix_config(conn, bike_id, value):
    conn.execute("UPDATE specs SET value=?, updated_at=datetime('now')"
                 " WHERE bike_id=? AND field_key='cylinder_configuration' AND value=?",
                 (value, bike_id, MIXED))


def _key(label):
    return re.sub(r"[^a-z0-9]+", "_",
                  label.lower().replace("×", " x ").replace("&", " and ")).strip("_")


def _make_two_stroke(conn, bike_id):
    """q5 -> A (2-stroke auto-lube), re-walked as the questionnaire does: the
    new branch's fields are added; empty four-stroke-only fields go."""
    answers = dict(conn.execute("SELECT question_id, option_label FROM bike_answers WHERE bike_id=?", (bike_id,)))
    if not answers or answers.get("q5") in ("A", "B"):
        return
    answers["q5"] = "A"
    _p, fields, _n, _t, used = questionnaire.run(answers)
    want = {_key(label) for label in fields}
    for qid, opt in used.items():
        want.update(r[0] for r in conn.execute(
            "SELECT field_key FROM field_triggers WHERE question_id=? AND option_label=?", (qid, opt)))
    want.update(r[0] for r in conn.execute("SELECT field_key FROM spec_fields WHERE universal=1"))
    for key in want:
        if conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (key,)).fetchone():
            conn.execute("INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                         " VALUES (?,?,NULL,'pending')", (bike_id, key))
    for key in ("valve_clearance_intake", "valve_clearance_exhaust", "engine_oil_volume", "engine_oil_weight"):
        if key not in want:
            conn.execute("DELETE FROM specs WHERE bike_id=? AND field_key=? AND (value IS NULL OR TRIM(value)='')",
                         (bike_id, key))
    conn.execute("DELETE FROM bike_answers WHERE bike_id=?", (bike_id,))
    conn.executemany("INSERT INTO bike_answers (bike_id, question_id, option_label) VALUES (?,?,?)",
                     [(bike_id, q, o) for q, o in used.items()])


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        rv200 = _bike(conn, "RV200 VanVan")
        if not rv200 or conn.execute("SELECT year_start FROM bikes WHERE id=?", (rv200,)).fetchone()[0] != 1972:
            print("VanVan/JR80 years already corrected (or not in this catalogue)")
            return
        done = []

        _set_years(conn, rv200, 2002, 2020, 1)
        _fix_config(conn, rv200, "Single, air-cooled four-stroke")
        done.append("RV200 VanVan 2002-2020")

        revival = _bike(conn, "RV125 VanVan (1972-82)")
        if revival:
            conn.execute("UPDATE bikes SET model_code='RV125 VanVan' WHERE id=?", (revival,))
            conn.execute("UPDATE bike_names SET name='Suzuki RV125 VanVan' WHERE bike_id=? AND is_primary=1", (revival,))
            _set_years(conn, revival, 2003, 2016, 1)
            _fix_config(conn, revival, "Single, air-cooled four-stroke")
            done.append("RV125 VanVan 2003-2016")

        original = _bike(conn, "RV125")
        if original:
            _set_years(conn, original, 1972, 1982, 1)
            _fix_config(conn, original, "Single, air-cooled two-stroke")
            _make_two_stroke(conn, original)
            done.append("RV125 (two-stroke) 1972-1982")

        jr80 = _bike(conn, "JR80 (minis)")
        if jr80:
            _set_years(conn, jr80, 2001, None, 0)
            done.append("JR80 2001-present")

        conn.commit()
        print("corrected: " + "; ".join(done))
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
