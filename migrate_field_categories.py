"""
A field under extra headings on every bike, set by admin.

spec_field_categories -- site-wide counterpart of bike_spec_categories. The
page shows the field's pointer under each extra heading on every bike; a
bike's manager can add headings on their bike but not remove these. Idempotent.

Run:  py migrate_field_categories.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

TABLE = """
-- The same, site-wide: admin putting a field under extra headings on EVERY
-- bike. A bike's manager cannot undo one of these on their bike -- it is a
-- Spec Tree fact, like the home category -- but can add more of their own.
CREATE TABLE spec_field_categories (
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  category   TEXT    NOT NULL,
  placed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  placed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (field_key, category)
);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='spec_field_categories'").fetchone():
        print("spec_field_categories already exists")
    else:
        conn.executescript(TABLE)
        print("created spec_field_categories")
    conn.commit()
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
