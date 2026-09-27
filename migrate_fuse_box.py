"""
One spec for the whole fuse box.

A wiring diagram prints the box as a table: each fuse, its rating, and what
it feeds. That table is one fact about the bike, and it is what a rider is
actually looking at when a circuit goes dead -- not "what size is the starter
fuse" but "which of these twenty is the one for the horn".

The per-circuit fuse fields stay. They answer a different question: a rider
replacing the fan fuse specifically. This is the map.

Universal, because every bike with electrics has at least a main fuse, and a
bike with none is answered by leaving it empty rather than by not asking.

Idempotent.

Run:  py migrate_fuse_box.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

KEY = "fuses"
LABEL = "Fuses"
CATEGORY = "Electrical"
EXAMPLE = "ignition: mini 10A, headlight: mini 15A, horn: mini 10A"


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    have = conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (KEY,)).fetchone()
    if not have:
        # Last in Electrical, so it does not push the single-circuit fuse
        # fields down the sheet.
        top = conn.execute(
            "SELECT IFNULL(MAX(sort_order), 0) FROM spec_fields WHERE category=?",
            (CATEGORY,)).fetchone()[0]
        cols = {r[1] for r in conn.execute("PRAGMA table_info(spec_fields)")}
        fields = ["field_key", "label", "category", "spec_type", "sort_order",
                  "universal", "value_type"]
        values = [KEY, LABEL, CATEGORY, "fixed", top + 10, 1, "fuse"]
        if "example" in cols:
            fields.append("example")
            values.append(EXAMPLE)
        conn.execute(
            f"INSERT INTO spec_fields ({', '.join(fields)})"
            f" VALUES ({', '.join('?' * len(fields))})", values)

    cur = conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " SELECT id, ?, NULL, 'pending' FROM bikes", (KEY,))
    added = cur.rowcount
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (KEY,)).fetchone()[0]
    print(f"  {LABEL}: {'created' if not have else 'already there'}, "
          f"{added} row(s) added, on {n} bike(s)")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
