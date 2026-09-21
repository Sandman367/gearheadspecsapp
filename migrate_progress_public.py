"""
Hide an offline spec from the rider-facing counts as well as the page.

Taking a spec offline removes its row from a rider's spec sheet. Leaving the
progress counters counting it would then advertise a field nobody can see, and
"56 of 65 filled" would disagree with the 64 rows underneath it.

bike_spec_progress gains a parallel set of _public columns. The originals are
unchanged and still drive the manager dashboard, where an offline spec is very
much still the manager's workload. Views hold no data, so rebuilding one is not
a destructive operation.

Idempotent: the view is dropped and recreated from the definition in schema.sql,
so this file cannot drift from the schema it is meant to match.

Run:  py migrate_progress_public.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

VIEW_SQL = """CREATE VIEW bike_spec_progress AS
SELECT
  b.id AS bike_id,
  COUNT(s.id)                                          AS fields_triggered,
  SUM(CASE WHEN s.value IS NOT NULL AND s.value <> '' THEN 1 ELSE 0 END) AS specs_filled,
  SUM(CASE WHEN s.value IS NULL OR s.value = ''  THEN 1 ELSE 0 END)      AS specs_needed,
  SUM(CASE WHEN s.paused = 1 THEN 1 ELSE 0 END)        AS specs_offline,
  SUM(CASE WHEN COALESCE(s.paused,0) = 0 AND s.id IS NOT NULL THEN 1 ELSE 0 END)
                                                       AS fields_triggered_public,
  SUM(CASE WHEN COALESCE(s.paused,0) = 0 AND s.value IS NOT NULL AND s.value <> ''
           THEN 1 ELSE 0 END)                          AS specs_filled_public,
  SUM(CASE WHEN COALESCE(s.paused,0) = 0 AND s.id IS NOT NULL
                AND (s.value IS NULL OR s.value = '') THEN 1 ELSE 0 END)
                                                       AS specs_needed_public
FROM bikes b
LEFT JOIN specs s ON s.bike_id = b.id
GROUP BY b.id;"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    if not [r[1] for r in conn.execute("PRAGMA table_info(specs)") if r[1] == "paused"]:
        raise SystemExit("run migrate_spec_pause.py first — specs has no paused column")

    conn.execute("DROP VIEW IF EXISTS bike_spec_progress")
    conn.executescript(VIEW_SQL)
    conn.commit()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(bike_spec_progress)")]
    print("view rebuilt with columns:", ", ".join(cols))

    row = conn.execute(
        "SELECT SUM(specs_offline), SUM(fields_triggered), SUM(fields_triggered_public)"
        " FROM bike_spec_progress").fetchone()
    print(f"specs offline right now : {row[0] or 0}")
    print(f"fields, manager view    : {row[1] or 0}")
    print(f"fields, rider view      : {row[2] or 0}")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
