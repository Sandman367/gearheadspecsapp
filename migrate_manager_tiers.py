"""
Manager tiers: two columns the standing is computed with.

  users.gold_confirmed        -- admin's say-so for Gold (the rest is computed)
  value_flags.old_entered_by  -- who entered the value a fix replaced, so a
                                 wrong spec counts against its author

Idempotent.  Run:  py migrate_manager_tiers.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    if "gold_confirmed" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN gold_confirmed INTEGER NOT NULL DEFAULT 0"
                     " CHECK (gold_confirmed IN (0,1))")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(value_flags)")}
    if "old_entered_by" not in cols:
        conn.execute("ALTER TABLE value_flags ADD COLUMN old_entered_by INTEGER"
                     " REFERENCES users(id) ON DELETE SET NULL")
    conn.commit()
    conn.close()
    print("manager tier columns in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
