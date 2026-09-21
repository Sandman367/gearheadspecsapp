"""
Let a bike's manager lift a spec into that bike's hero.

The hero has shown the General category since it was introduced, and General is
a property of the field, so it is the same eight rows on all 261 bikes. That is
right for a default and wrong as the only option: which numbers someone checks
first depends on the machine. Final drive ratio belongs at the top of a dirt
bike, fuel range at the top of a tourer, and neither belongs at the top of the
other.

Stored per bike rather than by moving the field into General, for the same
reason the Fixed tag is per bike: General is a Spec Tree decision that lands on
every bike at once, and a manager is scoped to the bikes assigned to them. A
manager reshaping one bike's hero cannot be allowed to reshape 260 others.

Pins are additive to General, never subtractive. Unpinning removes only what
somebody pinned, so a mis-click costs one click and the site-wide default is
never at the mercy of a single bike's page.

Additive and idempotent.

Run:  py migrate_header_specs.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table'"
        " AND name='bike_header_specs'").fetchone()
    if existing:
        print("bike_header_specs already present — nothing to do")
    else:
        conn.executescript("""
            CREATE TABLE bike_header_specs (
              bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
              field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
              sort_order INTEGER NOT NULL DEFAULT 0,
              pinned_by  INTEGER REFERENCES users(id),
              pinned_at  TEXT    NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (bike_id, field_key)
            );
            CREATE INDEX idx_bike_header_specs_bike ON bike_header_specs (bike_id);
        """)
        conn.commit()
        print("bike_header_specs created")

    n = conn.execute("SELECT COUNT(*) FROM bike_header_specs").fetchone()[0]
    general = conn.execute(
        "SELECT COUNT(*) FROM spec_fields WHERE category='General'").fetchone()[0]
    print(f"pinned specs        : {n}")
    print(f"General fields      : {general}  (every bike's hero shows these anyway)")
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
