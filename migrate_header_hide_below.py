"""
Let a pinned spec be shown in the header INSTEAD of its section, not as well.

Pinning has always added to the header and left the spec where it was, because
the hero quicklist carries no vote, flag or request controls -- a spec that
appears only up there quietly loses all three. That is the right default and
the wrong rule: a headline figure nobody argues about reads better once, at the
top, and the manager is the one who knows which of their specs those are.

bike_header_specs gains hide_below, defaulting to 0, so every existing pin
keeps behaving exactly as it does today.

Additive and idempotent.

Run:  py migrate_header_hide_below.py [path/to/data.db]
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

    cols = {r[1] for r in conn.execute("PRAGMA table_info(bike_header_specs)")}
    if "hide_below" in cols:
        print("hide_below already present")
    else:
        conn.execute(
            "ALTER TABLE bike_header_specs ADD COLUMN hide_below INTEGER"
            " NOT NULL DEFAULT 0 CHECK (hide_below IN (0,1))")
        conn.commit()
        print("hide_below added")

    total = conn.execute("SELECT COUNT(*) FROM bike_header_specs").fetchone()[0]
    hidden = conn.execute(
        "SELECT COUNT(*) FROM bike_header_specs WHERE hide_below=1").fetchone()[0]
    print(f"pinned specs: {total} ({hidden} shown only in the header)")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
