"""
"Let me look after this bike." A rider offering to manage a bike nobody manages.

A bike with no manager shows a link on its sheet; the rider says what they
know about the machine and whether they own one, and admin approves (which
assigns them, exactly as Admin -> Assignments does) or declines with a note
the rider reads on the same page. One open application per rider per bike;
a rider can withdraw theirs while it is open. Idempotent.

Run:  py migrate_manager_applications.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
-- "Let me look after this bike." A rider offering to manage a bike nobody
-- manages. Admin approves (which assigns them, as Admin -> Assignments does)
-- or declines with a note the rider reads. One open application per rider
-- per bike; the rider can withdraw it while it is open.
CREATE TABLE IF NOT EXISTS manager_applications (
  id           INTEGER PRIMARY KEY,
  bike_id      INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  -- what they know about this bike, and how
  experience   TEXT    NOT NULL,
  owns_one     INTEGER NOT NULL DEFAULT 0 CHECK (owns_one IN (0,1)),
  status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','approved','declined','withdrawn')),
  decided_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  admin_note   TEXT,
  resolved_at  TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_manager_applications_status
  ON manager_applications (status, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_manager_applications_open
  ON manager_applications (bike_id, user_id) WHERE status = 'pending';
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    # "I own one" and "I used to own one" are two separate ticks.
    if "used_to_own" not in [r[1] for r in conn.execute("PRAGMA table_info(manager_applications)")]:
        conn.execute("ALTER TABLE manager_applications ADD COLUMN used_to_own INTEGER NOT NULL DEFAULT 0"
                     " CHECK (used_to_own IN (0,1))")
    conn.commit()
    conn.close()
    print("manager_applications in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
