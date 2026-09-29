"""
The Suzuki JR80 is a two-stroke (79cc, air-cooled, Suzuki CCIS auto-lube),
but its questionnaire answer q5 said C -- a four-stroke with valve
clearance adjustment -- so its sheet offered valve clearances and engine
oil it hasn't got, and none of the auto-lube specs it needs. See
BIKE_ERRORS.md, item 1.

This re-walks its answers with q5 = A (2-stroke auto-lube), exactly as the
questionnaire does: the new branch's fields are added, and the four-stroke
fields that no longer apply are removed only if nobody has filled them in.
A field with a value is never touched.

Acts only while the bike still says q5 = C, so it runs once and then does
nothing. Found by make and model, not by id.

Run:  py fix_jr80_two_stroke.py [path/to/data.db]
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import questionnaire  # noqa: E402

DEFAULT_DB = os.path.join(ROOT, "data.db")
MAKE, MODEL = "Suzuki", "JR80 (minis)"
# Four-stroke only: valve clearances, and engine oil (a two-stroke burns its
# oil; the gearbox has its own, which the new branch adds).
FOUR_STROKE_ONLY = ["valve_clearance_intake", "valve_clearance_exhaust",
                    "engine_oil_volume", "engine_oil_weight"]


def _key(label):
    return re.sub(r"[^a-z0-9]+", "_",
                  label.lower().replace("×", " x ").replace("&", " and ")).strip("_")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        row = conn.execute("SELECT id FROM bikes WHERE make=? AND model_code=?", (MAKE, MODEL)).fetchone()
        if not row:
            print("JR80 not in this catalogue; nothing to fix")
            return
        bike_id = row[0]
        answers = dict(conn.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?", (bike_id,)))
        if answers.get("q5") != "C":
            print("JR80 already corrected")
            return

        answers["q5"] = "A"
        _path, fields, _not_sure, _type, used = questionnaire.run(answers)
        want = {_key(label) for label in fields}
        for qid, opt in used.items():
            want.update(r[0] for r in conn.execute(
                "SELECT field_key FROM field_triggers WHERE question_id=? AND option_label=?", (qid, opt)))
        want.update(r[0] for r in conn.execute("SELECT field_key FROM spec_fields WHERE universal=1"))

        added = 0
        for key in sorted(want):
            if conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (key,)).fetchone():
                added += conn.execute(
                    "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                    " VALUES (?,?,NULL,'pending')", (bike_id, key)).rowcount
        removed = 0
        for key in FOUR_STROKE_ONLY:
            if key not in want:
                removed += conn.execute(
                    "DELETE FROM specs WHERE bike_id=? AND field_key=?"
                    " AND (value IS NULL OR TRIM(value)='')", (bike_id, key)).rowcount

        conn.execute("DELETE FROM bike_answers WHERE bike_id=?", (bike_id,))
        conn.executemany(
            "INSERT INTO bike_answers (bike_id, question_id, option_label) VALUES (?,?,?)",
            [(bike_id, q, o) for q, o in used.items()])
        conn.commit()
        print(f"JR80 (bike {bike_id}) is now a 2-stroke auto-lube: {added} spec(s) added, "
              f"{removed} empty four-stroke spec(s) removed")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
