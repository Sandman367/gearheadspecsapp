"""
An example value per field ("e.g. K&N KN-145").

Admin writes it once on the Spec Tree; it becomes the example in the entry
box on every bike, so values arrive in a consistent shape. Idempotent.

Run:  py migrate_field_example.py [path/to/data.db]
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(spec_fields)")}
    if "example" not in cols:
        conn.execute("ALTER TABLE spec_fields ADD COLUMN example TEXT")
    conn.commit()
    conn.close()
    print("spec_fields.example in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
