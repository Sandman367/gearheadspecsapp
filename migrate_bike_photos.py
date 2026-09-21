"""
A photo for each bike.

  * bike_photos -- one row per bike: the file under data/photos, its type and
    who supplied it. The page shows it in place of "Photo pending".
  * manager_notices admits kind 'photo', so admin hears when a manager puts a
    picture on a bike every rider will see. The CHECK is part of the column,
    so the table is copied; it is days old and holds a handful of rows.

Idempotent.

Run:  py migrate_bike_photos.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

PHOTOS = """
CREATE TABLE bike_photos (
  bike_id      INTEGER PRIMARY KEY REFERENCES bikes(id) ON DELETE CASCADE,
  file         TEXT    NOT NULL,
  mime         TEXT    NOT NULL CHECK (mime IN ('image/jpeg','image/png','image/webp')),
  bytes        INTEGER NOT NULL,
  uploaded_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  uploaded_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

NOTICES = """
CREATE TABLE manager_notices_new (
  id          INTEGER PRIMARY KEY,
  bike_id     INTEGER REFERENCES bikes(id) ON DELETE SET NULL,
  actor       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  kind        TEXT    NOT NULL CHECK (kind IN ('rename','split','photo')),
  summary     TEXT    NOT NULL,
  detail      TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  seen_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
  seen_at     TEXT
);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='bike_photos'").fetchone():
        print("bike_photos already exists")
    else:
        conn.executescript(PHOTOS)
        print("created bike_photos")

    sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='manager_notices'").fetchone()
    if sql is None:
        raise SystemExit("manager_notices is missing -- run migrate_bike_identity.py first")
    if "'photo'" in sql[0]:
        print("manager_notices already admits 'photo'")
    else:
        before = conn.execute("SELECT COUNT(*) FROM manager_notices").fetchone()[0]
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.executescript(NOTICES + """
            INSERT INTO manager_notices_new SELECT * FROM manager_notices;
            DROP TABLE manager_notices;
            ALTER TABLE manager_notices_new RENAME TO manager_notices;
            CREATE INDEX idx_manager_notices_unseen ON manager_notices (created_at)
              WHERE seen_at IS NULL;
        """)
        conn.execute("PRAGMA foreign_keys = ON")
        after = conn.execute("SELECT COUNT(*) FROM manager_notices").fetchone()[0]
        assert before == after, f"notice count changed {before}->{after}"
        print(f"manager_notices rebuilt ({after} rows): kind now admits 'photo'")

    os.makedirs(os.path.join(ROOT, "data", "photos"), exist_ok=True)
    conn.commit()
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
