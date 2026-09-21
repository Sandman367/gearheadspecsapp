"""
"This bike should list X." A rider asking for a field the bike does not have.

spec_requests already covers "fill this in" on a field the bike has. This is
the step before it: the field is on the Spec Tree but not on this bike --
the questionnaire never gave it one, or the bike came from a catalogue and
answered nothing -- and a rider wants it there. One row per person per
(bike, field), so the count is the people who asked; the bike's manager (or
admin) adds the field or declines, and every request for that pair resolves
together. A field that is not on the tree at all is a branch proposal, which
already exists; the request page files one of those instead. Idempotent.

Run:  py migrate_field_requests.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
CREATE TABLE IF NOT EXISTS field_requests (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key   TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  reasoning   TEXT,
  status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','added','declined')),
  decided_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  admin_note  TEXT,
  resolved_at TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, field_key, user_id)
);
CREATE INDEX IF NOT EXISTS idx_field_requests_bike ON field_requests (bike_id, status);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print("field_requests in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
