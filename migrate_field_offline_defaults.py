"""
"Starts offline on this kind of bike."

A field can be on a bike's sheet and still not belong there by default: a
scooter answers "belt drive", the belt branch carries the two sprocket fields
(a Harley's belt pulleys live there), and every CVT scooter ends up showing
sprockets it does not have. Rather than take them off the belt branch and
lose them for the Harleys, a rule per (field, bike type) says the row is
created OFFLINE on that type of bike. The manager can put it online on any
one machine that really has the part; nothing about the field changes.

Enforced by a trigger, so every path that creates a spec row -- the
questionnaire, admin's apply, "every bike", a rider's request, the import
scripts -- honours it without knowing about it. Seeds the first rule: Front
Sprocket and Rear Sprocket start offline on Scooters. Idempotent.

Run:  py migrate_field_offline_defaults.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
CREATE TABLE IF NOT EXISTS field_offline_defaults (
  field_key  TEXT NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  bike_type  TEXT NOT NULL,
  created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (field_key, bike_type)
);

CREATE TRIGGER IF NOT EXISTS trg_specs_default_offline
AFTER INSERT ON specs
WHEN EXISTS (
  SELECT 1 FROM field_offline_defaults d
  JOIN bikes b ON b.id = NEW.bike_id
  WHERE d.field_key = NEW.field_key AND d.bike_type = b.bike_type
)
BEGIN
  UPDATE specs SET paused = 1, paused_at = datetime('now') WHERE id = NEW.id;
END;
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    n = 0
    for key in ("front_sprocket", "rear_sprocket"):
        if conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (key,)).fetchone():
            n += conn.execute(
                "INSERT OR IGNORE INTO field_offline_defaults (field_key, bike_type) VALUES (?, 'Scooter')",
                (key,)).rowcount
    conn.commit()
    conn.close()
    print(f"field_offline_defaults in place; {n} rule(s) added")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
