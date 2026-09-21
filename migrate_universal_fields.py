"""
Mark a field as applying to every bike.

Some specs are not conditional on anything the questionnaire asks — road trip
tools, for instance, belong on every machine regardless of drive type or engine
layout. Until now the only ways a field reached a bike were a questionnaire
branch or being named bike by bike, so a universal field meant clicking through
260 bikes and doing it again for every bike added afterwards.

`universal` is a flag rather than 260 trigger rows for the same reason votes are
rows and counts are views: the intent is one fact, and deriving membership from
it means a bike created tomorrow is covered without anyone remembering to go
back. Back-filling the bikes that already exist is a separate, explicit step.

Additive and idempotent.

Run:  py migrate_universal_fields.py [path/to/data.db]
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

    cols = {r[1] for r in conn.execute("PRAGMA table_info(spec_fields)")}
    if "universal" in cols:
        print("already migrated — spec_fields.universal exists")
    else:
        conn.execute(
            "ALTER TABLE spec_fields ADD COLUMN universal INTEGER NOT NULL DEFAULT 0"
            " CHECK (universal IN (0,1))")
        conn.commit()
        print("added: spec_fields.universal (0 = conditional, 1 = every bike)")

    n = conn.execute("SELECT COUNT(*) FROM spec_fields WHERE universal=1").fetchone()[0]
    bikes = conn.execute("SELECT COUNT(*) FROM bikes").fetchone()[0]
    print(f"universal fields: {n}")
    print(f"bikes in database: {bikes}  (each universal field costs one spec row per bike)")
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
