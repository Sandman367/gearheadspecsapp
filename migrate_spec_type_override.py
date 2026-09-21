"""
Let a bike manager mark one spec, on their own bike, as a Fixed spec.

The problem this solves: spec_type lives on spec_fields, which is platform
wide. A manager toggling it there would change that field on every bike in the
database — exactly the authority the permission model reserves for admin
("admin owns the tree, the manager owns the values").

So the override goes on the specs row, which is per bike. The effective type is

    COALESCE(specs.spec_type, spec_fields.spec_type)

NULL means "inherit the platform default", which is also how a manager clears
their override — the field goes back to whatever the tree says, rather than
being stuck at whatever they last chose.

Additive and idempotent.

Run:  py migrate_spec_type_override.py [path/to/data.db]
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

    cols = {r[1] for r in conn.execute("PRAGMA table_info(specs)")}
    if "spec_type" in cols:
        print("already migrated — specs.spec_type exists")
    else:
        # SQLite cannot add a column with a CHECK referencing itself in older
        # versions, but a plain CHECK on the new column is fine.
        conn.execute(
            "ALTER TABLE specs ADD COLUMN spec_type TEXT"
            " CHECK (spec_type IS NULL OR spec_type IN ('fixed','pref','community'))")
        conn.commit()
        print("added: specs.spec_type (per-bike override, NULL = inherit)")

    n = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE spec_type IS NOT NULL").fetchone()[0]
    print(f"overrides set: {n}")
    print("effective-type check (first 5 specs on the CB919):")
    for r in conn.execute(
            "SELECT f.label, COALESCE(s.spec_type, f.spec_type) AS eff,"
            "       s.spec_type IS NOT NULL AS overridden"
            " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
            " WHERE s.bike_id = (SELECT id FROM bikes WHERE model_code='CB900F2 919')"
            " ORDER BY f.sort_order LIMIT 5"):
        print(f"   {r[0]:26} {r[1]:10} {'(overridden)' if r[2] else ''}")

    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
