"""
Visitor counts for Admin -> Visitors.

One row per visitor per day, keyed by a hash of their address and browser
salted with that day's random salt. The salt is deleted when the day ends,
so yesterday's hashes can never be recomputed or linked to today's: a
person is counted once a day and cannot be followed from day to day. No
address or cookie is stored. Idempotent.

Run:  py migrate_visits.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
-- Visitor counts for Admin -> Visitors. One row per visitor per day; the
-- visitor is a hash salted with that day's salt, and the salt is deleted
-- when the day ends, so nobody can be followed from one day to the next.
-- referrer/country/landing are from the first page they opened that day.
CREATE TABLE IF NOT EXISTS visits (
  day      TEXT    NOT NULL,              -- YYYY-MM-DD, UTC
  visitor  TEXT    NOT NULL,
  referrer TEXT    NOT NULL DEFAULT '',   -- '' = typed it or a bookmark
  country  TEXT    NOT NULL DEFAULT '',   -- two-letter code, '' = unknown
  landing  TEXT    NOT NULL DEFAULT '',
  pages    INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (day, visitor)
);

-- Which bikes people open: one row per visitor per bike per day.
CREATE TABLE IF NOT EXISTS bike_views (
  day      TEXT    NOT NULL,
  bike_id  INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  visitor  TEXT    NOT NULL,
  PRIMARY KEY (day, bike_id, visitor)
);
CREATE INDEX IF NOT EXISTS idx_bike_views_bike ON bike_views (bike_id, day);

-- Today's salt only; older ones are deleted as each new day starts.
CREATE TABLE IF NOT EXISTS visit_salts (
  day  TEXT PRIMARY KEY,
  salt TEXT NOT NULL
);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print("visits in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
