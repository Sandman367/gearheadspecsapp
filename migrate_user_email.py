"""
An email address on each account, asked for at registration, admin's eyes only.

Adds users.email (NULL on every existing account) and a unique index on it,
case-insensitive, so one address cannot register twice. Idempotent.

Run:  py migrate_user_email.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
    if "email" in cols:
        print("users.email already exists")
    else:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        print("added users.email")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email"
                 " ON users (email COLLATE NOCASE) WHERE email IS NOT NULL")
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM users WHERE email IS NULL").fetchone()[0]
    print(f"accounts without an email (registered before this): {n}")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
