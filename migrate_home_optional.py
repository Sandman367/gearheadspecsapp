"""
The home heading becomes optional.

Both placement tables gain `shown`. A row with shown=0 is written only for the
field's home heading and means "not under its home" -- on that bike
(bike_spec_categories) or everywhere (spec_field_categories). Every existing
row is an extra heading and stays shown=1. Idempotent.

Run:  py migrate_home_optional.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    for table in ("bike_spec_categories", "spec_field_categories"):
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if "shown" in cols:
            print(f"{table}.shown already exists")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN shown INTEGER NOT NULL DEFAULT 1"
                         " CHECK (shown IN (0,1))")
            print(f"added {table}.shown")
    conn.commit()
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
