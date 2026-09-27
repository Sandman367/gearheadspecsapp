"""
The wiring diagram, once per bike, shown on every wire it explains.

A wire colour tells a rider what to look for. The diagram tells them where
it goes -- and a rider tracing a circuit wants both in front of them at the
same time. Links already exist per (bike, field), which is right for a
YouTube video about one spec and wrong for a diagram that answers every
electrical spec on the machine, since it would have to be pasted onto each
one and kept in step by hand.

So the diagram belongs to the bike. It is added once by that bike's manager
and appears on every wire-colour spec the bike carries.

Idempotent.

Run:  py migrate_wiring_diagrams.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
CREATE TABLE IF NOT EXISTS wiring_diagrams (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  title      TEXT    NOT NULL,
  url        TEXT    NOT NULL,
  -- Which years this diagram covers, when a bike spans a change. Both NULL
  -- means every year the bike covers, which is what most of them are.
  year_from  INTEGER,
  year_to    INTEGER,
  added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, url)
);
CREATE INDEX IF NOT EXISTS idx_wiring_diagrams_bike ON wiring_diagrams (bike_id, paused);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print("wiring_diagrams in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
