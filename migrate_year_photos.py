"""
Photos per model year.

bike_photos held one photo per bike, keyed on bike_id. It now holds a main
photo (year NULL) and up to one photo per model year, so a later manager can
show the year their own bike is while the first manager's photo keeps
standing for the rest. The old row becomes the main photo. Idempotent.

Run:  py migrate_year_photos.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bike_photos)")}
    if "year" in cols:
        conn.close()
        print("bike_photos already per year")
        return
    conn.executescript("""
    BEGIN;
    CREATE TABLE bike_photos_new (
      id           INTEGER PRIMARY KEY,
      bike_id      INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
      year         INTEGER,
      file         TEXT    NOT NULL,
      mime         TEXT    NOT NULL CHECK (mime IN ('image/jpeg','image/png','image/webp')),
      bytes        INTEGER NOT NULL,
      uploaded_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
      uploaded_at  TEXT    NOT NULL DEFAULT (datetime('now')),
      UNIQUE (bike_id, year)
    );
    INSERT INTO bike_photos_new (bike_id, year, file, mime, bytes, uploaded_by, uploaded_at)
      SELECT bike_id, NULL, file, mime, bytes, uploaded_by, uploaded_at FROM bike_photos;
    DROP TABLE bike_photos;
    ALTER TABLE bike_photos_new RENAME TO bike_photos;
    CREATE UNIQUE INDEX idx_bike_photos_main ON bike_photos (bike_id) WHERE year IS NULL;
    COMMIT;
    """)
    conn.close()
    print("bike_photos now holds a main photo and one per model year")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
