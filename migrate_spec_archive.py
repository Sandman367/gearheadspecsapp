"""
An archive for specs a bike does not need on its sheet.

Offline already hides a spec from riders, but its manager keeps seeing it
inline -- they have to, or they could never put it back. That is right for a
value being checked this week and wrong for one that is simply not part of
this machine: a kickstand switch on a bike that has none, a carb field on a
bike somebody converted. Those sit on the manager's sheet forever with a
"Put back" button nobody is ever going to press.

Archived is the third state. Gone from the sheet for everyone, including the
manager, and reachable through one button that only appears when there is
something behind it. Nothing is deleted: the value, its alternates, its votes
and its flags all survive, and putting it back restores it whole.

Idempotent.

Run:  py migrate_spec_archive.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    # The same three columns on both tables: a whole spec can be archived,
    # and so can one alternative on a spec that stays. Hiding an alternative
    # already keeps it on the manager's own sheet -- they need that to put it
    # back -- which leaves a wrong suggestion sitting in front of them forever.
    for table in ("specs", "spec_alternates"):
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "archived" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN archived_by INTEGER REFERENCES users(id)")
            conn.execute(f"ALTER TABLE {table} ADD COLUMN archived_at TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_specs_archived"
                 " ON specs (bike_id, archived)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_alternates_archived"
                 " ON spec_alternates (spec_id, archived)")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM specs WHERE archived=1").fetchone()[0]
    a = conn.execute("SELECT COUNT(*) FROM spec_alternates WHERE archived=1").fetchone()[0]
    print(f"archived in place ({n} spec(s), {a} alternative(s))")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
