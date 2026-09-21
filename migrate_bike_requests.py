"""
"Add my bike." A rider asking for a machine the catalogue does not have.

The request carries what a rider knows -- make, model, years, engine size,
kind of bike -- and a reason. Admin creates the bike from it, or points the
request at a bike that was already there under another name, or declines
with a note the riders can read. Riders who want the same bike add their
name to the open request rather than filing another, so the count is the
people waiting and one decision settles all of them. Idempotent.

Run:  py migrate_bike_requests.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
-- "Add my bike." A rider asking for a machine the catalogue does not have.
-- The request carries what a rider knows -- make, model, years, engine size,
-- kind of bike -- and a reason; admin creates the bike from it (or points
-- the request at a bike that was already there under another name) or
-- declines with a note the riders can read. Riders who want the same bike
-- add their name to the open request rather than filing another, so the
-- count is the people waiting, and one decision settles all of them.
CREATE TABLE IF NOT EXISTS bike_requests (
  id           INTEGER PRIMARY KEY,
  user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  make         TEXT    NOT NULL,
  model        TEXT    NOT NULL,
  year_from    INTEGER,
  year_to      INTEGER,
  displacement TEXT,
  bike_type    TEXT,
  reasoning    TEXT,
  source_url   TEXT,
  status       TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','added','declined')),
  -- the bike it became, or was matched to; NULL until then, or if declined
  bike_id      INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  decided_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  admin_note   TEXT,
  resolved_at  TEXT,
  created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_bike_requests_status ON bike_requests (status, created_at);

-- Everyone waiting on a request, the rider who filed it included.
CREATE TABLE IF NOT EXISTS bike_request_supporters (
  request_id INTEGER NOT NULL REFERENCES bike_requests(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (request_id, user_id)
);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print("bike_requests in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
