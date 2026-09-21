"""
Minimum Fuel Octane and Max Ethanol as value types.

Decisions (Sep 2026):
  1. The existing field keeps its key. `fuel_octane_grade` is what specs and
     header pins point at; only the label changes, to "Minimum Fuel Octane".
  2. Stored as "AKI:91" / "RON:95" -- the grade the manual named, in the system
     the manual used -- never a row id and never the converted figure.
  3. Ethanol is its own field, `max_ethanol`. The two are entered together on
     the page, but they are different facts with opposite shapes: octane is a
     minimum the engine needs, ethanol is a maximum the fuel system tolerates.
  4. Formula-derived equivalents are shown, labelled as estimates.
  5. An alternate below the stated minimum is allowed; the votes sort it out.

What this does:
  * Rebuilds spec_fields so value_type admits 'fuel_octane' and 'ethanol'.
    The CHECK is part of the column definition and SQLite cannot alter it, so
    the table is copied. field_key is the primary key and is carried over
    unchanged, so specs, field_triggers and bike_header_specs stay attached.
  * Relabels and retypes the octane field.
  * Registers max_ethanol beside it and gives it to every bike that already
    carries the octane field -- the two are meant to be answered together.
  * Converts what was typed: '87' becomes 'AKI:87'. Anything that cannot be
    read as a grade is reported and left exactly as it is.

As first written this also gated both fields behind a new questionnaire
question, q4b "What does this bike run on?", so an electric bike would never be
asked for an octane grade. That was reverted the same day -- every bike in the
catalog runs on gasoline, so the question never changed anything -- and the
gating was taken out of here so a re-run cannot put it back. The reversal, and
the universal flag on both fields, is migrate_fuel_universal.py.

Idempotent throughout.

Run:  py migrate_fuel_octane.py [path/to/data.db]
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
import fuel_octane

DEFAULT_DB = os.path.join(ROOT, "data.db")

OCTANE = "fuel_octane_grade"
ETHANOL = "max_ethanol"
VALUE_TYPES = ("text", "wire_color", "fuel_octane", "ethanol")


def rebuild_spec_fields(conn):
    """Widen the value_type CHECK. Copies the table, keeping field_key."""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='spec_fields'").fetchone()[0]
    if "'fuel_octane'" in sql:
        print("spec_fields already admits fuel_octane")
        return
    new_check = "CHECK (value_type IN (%s))" % ", ".join(f"'{t}'" for t in VALUE_TYPES)
    new_sql = re.sub(r"CHECK \(value_type IN \([^)]*\)\)", new_check, sql)
    assert new_sql != sql, "could not find the value_type CHECK to widen"
    new_sql = new_sql.replace("CREATE TABLE spec_fields", "CREATE TABLE spec_fields_new", 1)

    cols = [r[1] for r in conn.execute("PRAGMA table_info(spec_fields)")]
    col_list = ", ".join(cols)
    views = [(r[0], r[1]) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")]
    triggers = [(r[0], r[1]) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")]
    indexes = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='spec_fields'"
        " AND sql IS NOT NULL")]

    before = conn.execute("SELECT COUNT(*) FROM spec_fields").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    try:
        for name, _ in reversed(views):
            conn.execute(f"DROP VIEW IF EXISTS {name}")
        conn.execute(new_sql)
        conn.execute(f"INSERT INTO spec_fields_new ({col_list}) SELECT {col_list} FROM spec_fields")
        conn.execute("DROP TABLE spec_fields")
        conn.execute("ALTER TABLE spec_fields_new RENAME TO spec_fields")
        for sql_i in indexes:
            conn.execute(sql_i)
        for _, sql_v in views:
            conn.execute(sql_v)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")
    after = conn.execute("SELECT COUNT(*) FROM spec_fields").fetchone()[0]
    assert before == after, f"spec_fields row count changed {before}->{after}"
    orphans = conn.execute(
        "SELECT COUNT(*) FROM specs s LEFT JOIN spec_fields f ON f.field_key=s.field_key"
        " WHERE f.field_key IS NULL").fetchone()[0]
    assert orphans == 0, f"{orphans} spec rows lost their field"
    print(f"spec_fields rebuilt: {after} fields, value_type now admits {', '.join(VALUE_TYPES)}")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    rebuild_spec_fields(conn)

    # ---- the octane field: relabel, retype ---------------------------------
    row = conn.execute("SELECT * FROM spec_fields WHERE field_key=?", (OCTANE,)).fetchone()
    if row is None:
        raise SystemExit(f"{OCTANE} is not in spec_fields — nothing to convert")
    conn.execute(
        "UPDATE spec_fields SET label='Minimum Fuel Octane', value_type='fuel_octane'"
        " WHERE field_key=?", (OCTANE,))
    print(f"{OCTANE}: label -> Minimum Fuel Octane, type -> fuel_octane")

    # ---- ethanol, beside it ------------------------------------------------
    if not conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (ETHANOL,)).fetchone():
        conn.execute(
            "INSERT INTO spec_fields (field_key, label, category, spec_type, sort_order,"
            " universal, value_type) VALUES (?,?,?,?,?,?,'ethanol')",
            (ETHANOL, "Max Ethanol", row["category"], "pref", row["sort_order"] + 5,
             row["universal"]))
        print(f"{ETHANOL} registered beside it as Max Ethanol")
    else:
        conn.execute("UPDATE spec_fields SET value_type='ethanol' WHERE field_key=?", (ETHANOL,))

    # ---- every bike carrying octane gets ethanol too ----------------------
    cur = conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " SELECT bike_id, ?, NULL, 'pending' FROM specs WHERE field_key=?",
        (ETHANOL, OCTANE))
    print(f"max_ethanol added to {cur.rowcount} bikes that already carry the octane field")
    conn.commit()

    # ---- convert what was typed -------------------------------------------
    converted, kept = 0, []
    for r in conn.execute(
            "SELECT id, bike_id, value FROM specs WHERE field_key=?"
            " AND value IS NOT NULL AND TRIM(value)<>''", (OCTANE,)).fetchall():
        try:
            canon = fuel_octane.normalise_octane(r["value"])
        except fuel_octane.FuelValueError as e:
            kept.append((r["bike_id"], r["value"], str(e)))
            continue
        if canon != r["value"]:
            conn.execute("UPDATE specs SET value=? WHERE id=?", (canon, r["id"]))
            print(f"  bike {r['bike_id']}: {r['value']!r} -> {canon!r}")
            converted += 1
    for r in conn.execute(
            "SELECT a.id, a.text FROM spec_alternates a JOIN specs s ON s.id=a.spec_id"
            " WHERE s.field_key=?", (OCTANE,)).fetchall():
        try:
            canon = fuel_octane.normalise_octane(r["text"])
        except fuel_octane.FuelValueError as e:
            kept.append(("alt", r["text"], str(e)))
            continue
        if canon != r["text"]:
            conn.execute("UPDATE spec_alternates SET text=? WHERE id=?", (canon, r["id"]))
            converted += 1
    conn.commit()
    print(f"values converted: {converted}")
    if kept:
        print(f"left alone (not readable as a grade): {len(kept)}")
        for bike, val, why in kept:
            print(f"    bike {bike}: {val!r} — {why}")

    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
