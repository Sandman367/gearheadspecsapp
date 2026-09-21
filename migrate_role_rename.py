"""
One-time migration: rename the 'public' role to 'user'.

Why a script and not just a re-seed: data.db holds real changes now — manager
assignments, community-submitted values, resolved flags — and seed.py would
destroy all of it.

Why a table rebuild: the role is constrained by
    CHECK (role IN ('public','manager','admin'))
and SQLite cannot alter a CHECK constraint in place. The documented way is the
12-step rebuild — new table, copy, drop, rename — which is what this does,
inside a transaction, with a foreign-key check before it commits.

'public' now means something different in this codebase: a visitor who is not
signed in at all. Leaving a stored role with the same name would make every
permission check ambiguous to read.

Run:  py migrate_role_rename.py [path/to/data.db]
Safe to run twice — it detects an already-migrated database and stops.
"""
import os
import shutil
import sqlite3
import sys
from datetime import datetime

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    ddl = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if ddl is None:
        raise SystemExit("this database has no users table")
    if "'user'" in ddl["sql"] and "'public'" not in ddl["sql"]:
        print("already migrated — nothing to do")
        conn.close()
        return

    counts = {r["role"]: r["n"] for r in conn.execute(
        "SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    print("before:", counts)

    backup = f"{db_path}.bak-{datetime.now():%Y%m%d-%H%M%S}"
    shutil.copy2(db_path, backup)
    print(f"backup: {os.path.basename(backup)}")

    # Foreign keys off for the rebuild, and it must be outside a transaction to
    # take effect. sessions, bike_managers, specs.entered_by and others all
    # point at users(id); the ids are preserved, so nothing actually breaks —
    # but SQLite would refuse the DROP while they reference it.
    # Views that select from users (user_profile_stats does) are validated when
    # the schema changes, so DROP TABLE users leaves them dangling and the
    # rename fails. Capture every view definition, drop them all, and put them
    # back afterwards — reading the SQL from sqlite_master rather than
    # hardcoding it, so a view added later is carried through too.
    views = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL"
    ).fetchall()
    print(f"views to rebuild: {len(views)}")

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.execute("BEGIN")
        for v in views:
            conn.execute(f'DROP VIEW IF EXISTS "{v["name"]}"')
        conn.execute("""
            CREATE TABLE users_new (
              id            INTEGER PRIMARY KEY,
              username      TEXT    NOT NULL UNIQUE,
              display_name  TEXT,
              role          TEXT    NOT NULL DEFAULT 'user'
                              CHECK (role IN ('user','manager','admin')),
              password_hash TEXT    NOT NULL,
              password_salt TEXT    NOT NULL,
              suspended     INTEGER NOT NULL DEFAULT 0 CHECK (suspended IN (0,1)),
              created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
            )""")
        conn.execute("""
            INSERT INTO users_new
              (id, username, display_name, role, password_hash, password_salt,
               suspended, created_at)
            SELECT id, username, display_name,
                   CASE role WHEN 'public' THEN 'user' ELSE role END,
                   password_hash, password_salt, suspended, created_at
            FROM users""")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users_new RENAME TO users")

        for v in views:
            conn.execute(v["sql"])

        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            conn.execute("ROLLBACK")
            raise SystemExit(f"foreign keys broke, rolled back: {broken[:5]}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")

    counts = {r["role"]: r["n"] for r in conn.execute(
        "SELECT role, COUNT(*) AS n FROM users GROUP BY role")}
    print("after: ", counts)

    # Prove the new constraint is live rather than assuming the DDL took.
    try:
        conn.execute("BEGIN")
        conn.execute("UPDATE users SET role='public' WHERE id="
                     "(SELECT MIN(id) FROM users)")
        conn.execute("ROLLBACK")
        raise SystemExit("CHECK constraint is NOT enforcing — investigate")
    except sqlite3.IntegrityError:
        conn.execute("ROLLBACK")
        print("verified: role='public' is now rejected")

    print(f"integrity: {conn.execute('PRAGMA integrity_check').fetchone()[0]}")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
