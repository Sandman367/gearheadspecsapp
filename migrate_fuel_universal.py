"""
Minimum Fuel Octane and Max Ethanol on every bike; the fuel-type question goes.

The octane rebuild (migrate_fuel_octane.py, earlier the same day) gated the two
fuel fields behind a new questionnaire question, q4b "What does this bike run
on?", so an electric bike would not be asked for an octane grade. The catalog
has no such bike: every machine in it runs on gasoline, and the question was a
click that never changed anything. Decision (Sep 2026): drop it.

What this does:
  * Marks fuel_octane_grade and max_ethanol universal, so they land on every
    bike at creation the way the other universal fields do.
  * Gives both to any bike that does not already carry them (none, at the time
    of writing -- the octane field had been universal before the gate and the
    rebuild put ethanol beside it -- but a bike built through the gate with a
    non-gasoline answer would be missing them).
  * Removes the q4b trigger rows, the q4b answers, and q4b itself from the
    questionnaire, re-pointing q4 and q4a at q5 where they went before. The
    alias that let q4b name the field goes with it: nothing names it now.
    Ids are never reused, so q4b stays retired if a fuel-type question is ever
    wanted again.

Idempotent throughout.

Run:  py migrate_fuel_universal.py [path/to/data.db]
"""
import io
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")
QFILE = os.path.join(ROOT, "data", "questionnaire.json")

FIELDS = ("fuel_octane_grade", "max_ethanol")
QID = "q4b"


def fix_questionnaire():
    doc = json.load(io.open(QFILE, encoding="utf-8"))
    Q = doc["questions"]
    if QID not in Q:
        print(f"{QID} already gone from the questionnaire")
        return
    after = Q[QID]["next"]                     # q5: where q4/q4a went before
    for qid in ("q4", "q4a"):
        nxt = Q[qid].get("next")
        if isinstance(nxt, list):
            for rule in nxt:
                if rule.get("goto") == QID:
                    rule["goto"] = after
        elif nxt == QID:
            Q[qid]["next"] = after
    del Q[QID]
    if QID in doc["primary_order"]:
        doc["primary_order"].remove(QID)
    doc["aliases"].pop("Minimum Fuel Octane", None)
    io.open(QFILE, "w", encoding="utf-8").write(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    print(f"{QID} removed; q4 and q4a go to {after} again")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    have = {r[0] for r in conn.execute(
        "SELECT field_key FROM spec_fields WHERE field_key IN (?,?)", FIELDS)}
    missing = [k for k in FIELDS if k not in have]
    if missing:
        raise SystemExit(f"not in spec_fields: {', '.join(missing)} "
                         "-- run migrate_fuel_octane.py first")

    cur = conn.execute(
        "UPDATE spec_fields SET universal=1 WHERE field_key IN (?,?) AND universal=0", FIELDS)
    print(f"marked universal: {cur.rowcount} field(s)")

    added = 0
    for key in FIELDS:
        cur = conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
            " SELECT b.id, ?, NULL, 'pending' FROM bikes b"
            " WHERE NOT EXISTS (SELECT 1 FROM specs s WHERE s.bike_id=b.id"
            "                   AND s.field_key=? AND s.year_from IS NULL)", (key, key))
        added += cur.rowcount
    print(f"bikes given a missing fuel field: {added}")

    cur = conn.execute("DELETE FROM field_triggers WHERE question_id=?", (QID,))
    print(f"{QID} trigger rows removed: {cur.rowcount}")
    cur = conn.execute("DELETE FROM bike_answers WHERE question_id=?", (QID,))
    print(f"{QID} answers removed: {cur.rowcount}")
    conn.commit()

    fix_questionnaire()

    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
