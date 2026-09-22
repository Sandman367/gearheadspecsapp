"""
Notes on a spec.

A manager writes a note on a spec of their own bike; admin writes one on the
field itself (bike_id NULL), which shows on every bike carrying that field.
One note per bike per field, one site-wide note per field.

Also widens manager_notices.kind to accept 'note', since a manager writing
one is a change admin hears about. Idempotent.

Run:  py migrate_spec_notes.py [path/to/data.db]
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
CREATE TABLE IF NOT EXISTS spec_notes (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  body       TEXT    NOT NULL,
  written_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_spec_notes_bike ON spec_notes (bike_id, field_key);
CREATE UNIQUE INDEX IF NOT EXISTS idx_spec_notes_one_per_bike ON spec_notes (bike_id, field_key) WHERE bike_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_spec_notes_one_site ON spec_notes (field_key) WHERE bike_id IS NULL;
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)

    row = conn.execute("SELECT sql FROM sqlite_master WHERE name='manager_notices'").fetchone()
    if row and "'note'" not in row[0]:
        # SQLite cannot alter a CHECK: rebuild the table from its own DDL with
        # the kind list widened, carry the rows across, swap it in.
        widened = row[0].replace("'rename','split','photo'", "'rename','split','photo','note'")
        widened = re.sub(r'CREATE TABLE ("?)manager_notices\1', 'CREATE TABLE manager_notices_new',
                         widened, count=1)
        conn.executescript(
            "PRAGMA foreign_keys=OFF;\nBEGIN;\n" + widened + ";\n"
            "INSERT INTO manager_notices_new SELECT * FROM manager_notices;\n"
            "DROP TABLE manager_notices;\n"
            "ALTER TABLE manager_notices_new RENAME TO manager_notices;\n"
            "COMMIT;\nPRAGMA foreign_keys=ON;")
        print("manager_notices.kind now accepts 'note'")
    conn.commit()
    conn.close()
    print("spec_notes in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
