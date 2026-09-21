"""
An audit trail for admin actions.

Nothing recorded who did what. Approving a proposal left a resolved_at on the
proposal and that was the extent of it; deleting a field with 63 sourced values
left no trace at all — the field, the rows and the values simply stopped
existing, with nothing to say they ever had.

So: one row per admin action, with a human-readable summary and a JSON detail.

`detail` matters most on the destructive actions. Deleting a field records the
values it destroyed, so a mistake is at least reconstructable by hand instead of
being gone. That is not a substitute for undo, and does not pretend to be — it
is the difference between "we can find out what was lost" and "we cannot".

Additive and idempotent.

Run:  py migrate_admin_activity.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS admin_actions (
         id         INTEGER PRIMARY KEY,
         user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
         action     TEXT    NOT NULL,   -- 'field.delete', 'proposal.approve', ...
         target     TEXT,               -- field key, bike id, username
         summary    TEXT    NOT NULL,   -- one sentence, already readable
         detail     TEXT,               -- JSON: counts, destroyed values, ids
         destructive INTEGER NOT NULL DEFAULT 0 CHECK (destructive IN (0,1)),
         created_at TEXT    NOT NULL DEFAULT (datetime('now'))
       )""",
    "CREATE INDEX IF NOT EXISTS idx_admin_actions_when ON admin_actions (created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_admin_actions_user ON admin_actions (user_id, created_at DESC)",
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    def objects():
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}

    before = objects()
    conn.execute("BEGIN")
    try:
        for sql in STATEMENTS:
            conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    added = sorted(objects() - before)
    print("added:", ", ".join(added) if added else "nothing (already migrated)")
    print("logged actions:", conn.execute("SELECT COUNT(*) FROM admin_actions").fetchone()[0])
    print()
    print("Note: the log starts empty. Actions taken before this migration were")
    print("never recorded and cannot be reconstructed.")
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
