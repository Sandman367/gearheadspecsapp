"""
Renaming a bike and splitting it at a model year.

Two things, both small:

  * manager_notices -- when a bike's MANAGER renames or splits their bike, the
    admin is told. A row here is a notice after the fact, not a request; it
    leaves the admin's queue when marked seen.

  * trg_bike_years_no_overlap_upd is re-created to exclude the row being
    updated. As written it compared a moving row against itself -- the row
    still sits on the old bike, same year, when BEFORE UPDATE runs -- so no
    model year could ever be moved from one bike to another, and a split is
    exactly that move. The INSERT trigger is untouched; it had no such row to
    trip on.

Idempotent.

Run:  py migrate_bike_identity.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

TRIGGER = """
CREATE TRIGGER trg_bike_years_no_overlap_upd
BEFORE UPDATE OF bike_id, year, market ON bike_years
FOR EACH ROW
WHEN EXISTS (
  SELECT 1
  FROM bike_years y
  JOIN bikes b  ON b.id = y.bike_id
  JOIN bikes nb ON nb.id = NEW.bike_id
  WHERE y.year = NEW.year
    AND y.market = NEW.market
    AND y.bike_id <> NEW.bike_id
    AND y.id <> OLD.id
    AND b.make = nb.make
    AND b.model_code = nb.model_code
)
BEGIN
  SELECT RAISE(ABORT, 'model-year already claimed by a sibling bike');
END;
"""

NOTICES = """
CREATE TABLE manager_notices (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  actor       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  kind        TEXT    NOT NULL CHECK (kind IN ('rename','split')),
  summary     TEXT    NOT NULL,
  detail      TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  seen_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  seen_at     TEXT
);
CREATE INDEX idx_manager_notices_unseen ON manager_notices (created_at) WHERE seen_at IS NULL;
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table'"
                    " AND name='manager_notices'").fetchone():
        print("manager_notices already exists")
    else:
        conn.executescript(NOTICES)
        print("created manager_notices")

    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='trigger'"
                       " AND name='trg_bike_years_no_overlap_upd'").fetchone()
    if sql and "y.id <> OLD.id" in sql[0]:
        print("year-move trigger already excludes the moving row")
    else:
        conn.execute("DROP TRIGGER IF EXISTS trg_bike_years_no_overlap_upd")
        conn.executescript(TRIGGER)
        print("re-created trg_bike_years_no_overlap_upd: a model year can now move between siblings")

    conn.commit()
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
