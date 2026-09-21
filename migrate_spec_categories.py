"""
A spec under more than one heading on a bike.

bike_spec_categories -- the extra headings a bike's manager put a field under
on that bike. The field keeps its one home category on the Spec Tree; the
page prints a pointer under each extra heading. Idempotent.

Run:  py migrate_spec_categories.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

TABLE = """
-- A spec shown under more than one heading on one bike.
--
-- Every field has one home category on the Spec Tree; that is where its row
-- lives and where votes, flags and edits happen. A bike's manager can ALSO
-- show it under other headings on that bike -- a spark plug under Electrical
-- as well as Engine -- and the page prints it there as a pointer to the home
-- row, never a second copy of the value. Per bike, like header pins, because
-- what reads naturally next to what depends on the machine.
CREATE TABLE bike_spec_categories (
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  category   TEXT    NOT NULL,
  placed_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  placed_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (bike_id, field_key, category)
);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bike_spec_categories'").fetchone():
        print("bike_spec_categories already exists")
    else:
        conn.executescript(TABLE)
        print("created bike_spec_categories")
    conn.commit()
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
