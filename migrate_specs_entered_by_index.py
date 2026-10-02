"""
An index on specs.entered_by.

Everything that counts a member's work -- the profile stats view behind
Admin -> Members, the manager tiers, the badges -- asks "which specs did this
person enter?". With no index that is a read of every spec row (160,000+) per
member, several times over, and Admin fires those requests in parallel: on
the live server it ran long enough under load to come back as 502s. With the
index each lookup touches only that member's rows. Idempotent.

Run:  py migrate_specs_entered_by_index.py [path/to/data.db]
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
    conn.execute("CREATE INDEX IF NOT EXISTS idx_specs_entered_by ON specs (entered_by)")
    conn.commit()
    conn.close()
    print("specs.entered_by index in place")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
