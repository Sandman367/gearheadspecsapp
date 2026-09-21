"""
A composite (bike_id, field_key) index on specs.

The catalog's model filter looks up one field for every bike. With only the
single-column indexes SQLite walked every row of that field once per bike,
which was nine seconds once the model lists went in. Idempotent.

Run:  py migrate_specs_bike_field_index.py [path/to/data.db]
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_specs_bike_field ON specs (bike_id, field_key)")
    conn.commit()
    conn.close()
    print("idx_specs_bike_field in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
