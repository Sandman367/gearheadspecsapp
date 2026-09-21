"""
Add spec_votes and spec_requests to an existing database.

Purely additive — two new tables and two views, no existing table touched — so
unlike the role rename this needs no table rebuild and no backup dance. It is
idempotent: run it as often as you like.

Run:  py migrate_add_votes_requests.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS spec_votes (
         spec_id    INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
         user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         created_at TEXT    NOT NULL DEFAULT (datetime('now')),
         PRIMARY KEY (spec_id, user_id)
       )""",
    """CREATE TABLE IF NOT EXISTS spec_requests (
         spec_id    INTEGER NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
         user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
         created_at TEXT    NOT NULL DEFAULT (datetime('now')),
         PRIMARY KEY (spec_id, user_id)
       )""",
    "CREATE INDEX IF NOT EXISTS idx_spec_requests_spec ON spec_requests (spec_id)",
    """CREATE VIEW IF NOT EXISTS spec_vote_counts AS
         SELECT s.id AS spec_id, COUNT(v.user_id) AS votes
         FROM specs s
         LEFT JOIN spec_votes v ON v.spec_id = s.id
         GROUP BY s.id""",
    """CREATE VIEW IF NOT EXISTS spec_request_counts AS
         SELECT s.id AS spec_id, COUNT(r.user_id) AS requests
         FROM specs s
         LEFT JOIN spec_requests r ON r.spec_id = s.id
         GROUP BY s.id""",
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    def objects():
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view','index')")}

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

    # The views must actually answer, not merely exist.
    for view in ("spec_vote_counts", "spec_request_counts"):
        n = conn.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
        print(f"  {view:20} covers {n} specs")

    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
