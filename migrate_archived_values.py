"""
Archiving one VALUE, not the spec it sits on.

Archiving a spec takes the whole row off the sheet, which is right when the
machine does not have that part. It is wrong for the far commoner case: the
spec belongs on the bike and the number in it is wrong. Taking the spec away
to get rid of a bad value hides the question along with the bad answer, and
the gap stops being visible as a gap.

So a stock value can be archived on its own. The spec stays, reads as not yet
sourced, and anybody can fill it again -- while the old value, who entered it
and when are kept here, so the decision is reversible and the person who put
it there is not erased.

Idempotent.

Run:  py migrate_archived_values.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
CREATE TABLE IF NOT EXISTS archived_values (
  id           INTEGER PRIMARY KEY,
  spec_id      INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
  value        TEXT    NOT NULL,
  confidence   TEXT,
  entered_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  value_source TEXT,
  archived_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  archived_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_archived_values_spec ON archived_values (spec_id);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM archived_values").fetchone()[0]
    print(f"  archived_values in place ({n} kept)")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
