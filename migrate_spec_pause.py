"""
Let a bike's manager take one spec offline.

Tools and links have been pausable since the schema was written — "a manager
hiding an entry without destroying it: a dead video link stops showing but the
row and its votes survive if it comes back". The spec's own value never was,
which left the one case that matters most with no answer: a rider flags a torque
figure as wrong, and until the manager finishes checking it, the wrong number
keeps being read as fact. The choices were to leave it up or delete it, and
deleting takes the value, its alternates and their votes with it.

Paused means the field still appears on the spec sheet — riders should know it
exists — but shows a notice where the value would be, and stops accepting votes,
flags and submissions until it is back online. Nothing is destroyed.

paused_by and paused_at are recorded because admin_actions covers admin work
only; when a manager withholds a value from the public, this row is the only
trace of who did it and when.

Additive and idempotent.

Run:  py migrate_spec_pause.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

COLUMNS = [
    ("paused",    "INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1))"),
    ("paused_by", "INTEGER REFERENCES users(id) ON DELETE SET NULL"),
    ("paused_at", "TEXT"),
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    have = {r[1] for r in conn.execute("PRAGMA table_info(specs)")}
    added = []
    for name, decl in COLUMNS:
        if name in have:
            continue
        # SQLite cannot add a column with a non-constant default, but every
        # default here is constant, so a plain ADD COLUMN is enough.
        conn.execute(f"ALTER TABLE specs ADD COLUMN {name} {decl}")
        added.append(name)
    conn.commit()

    print("columns added:", ", ".join(added) if added else "none (already present)")

    paused = conn.execute("SELECT COUNT(*) FROM specs WHERE paused=1").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM specs").fetchone()[0]
    print(f"specs offline     : {paused} of {total}")
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
