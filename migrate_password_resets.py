"""
Password reset links.

A rider who forgets their password has no way back into the site: the only
routes to a new one are knowing the old one, or admin setting it by hand.
This is the table behind the emailed link that fixes that.

The token is never stored. What is stored is its SHA-256, the same reason
a password is not stored: a stolen copy of this table must not let anybody
walk into an account. A row is single use, expires in an hour, and every
outstanding row for a user is spent the moment one of them is.

Idempotent.

Run:  py migrate_password_resets.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

DDL = """
CREATE TABLE IF NOT EXISTS password_resets (
  id         INTEGER PRIMARY KEY,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash TEXT    NOT NULL UNIQUE,
  expires_at TEXT    NOT NULL,
  used_at    TEXT,
  created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_password_resets_user ON password_resets (user_id, used_at);
CREATE INDEX IF NOT EXISTS idx_password_resets_expiry ON password_resets (expires_at);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(DDL)
    conn.commit()
    conn.close()
    print("password_resets in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
