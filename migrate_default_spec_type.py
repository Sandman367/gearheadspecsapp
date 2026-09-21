"""
Make "Fixed spec" opt-in rather than the default.

111 of 120 fields were marked `fixed` — not because anyone judged them, but
because 'fixed' was the default in the schema, the seeder and the approve
dialog. A tag that nearly every field carries says nothing, and it was actively
in the way: `fixed` refuses alternates, so the blanket default quietly closed
community contribution across almost the whole tree.

The new default is `pref` — "the manual gives a baseline, riders legitimately
vary" — which permits alternates while still implying a factory value exists.
`fixed` now means somebody decided this field has exactly one right answer.

This flips the 111 inherited `fixed` fields to `pref`. The 4 `community` fields
are left alone: that is a different, deliberate statement.

Note this does NOT touch specs.spec_type, the per-bike override a bike's
manager sets to lock one spec. That tag is unaffected and still means what it
meant.

Run:  py migrate_default_spec_type.py [path/to/data.db]
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

    before = {r[0]: r[1] for r in conn.execute(
        "SELECT spec_type, COUNT(*) FROM spec_fields GROUP BY spec_type")}
    print("before:", before)

    overrides = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE spec_type IS NOT NULL").fetchone()[0]

    conn.execute("BEGIN")
    try:
        changed = conn.execute(
            "UPDATE spec_fields SET spec_type='pref' WHERE spec_type='fixed'").rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    after = {r[0]: r[1] for r in conn.execute(
        "SELECT spec_type, COUNT(*) FROM spec_fields GROUP BY spec_type")}
    print("after :", after)
    print(f"flipped {changed} field(s) from fixed to pref")
    print()
    print(f"per-bike manager 'Fixed' tags, untouched: {overrides}")
    print("Alternates are now accepted on every field that was previously")
    print("closed by the inherited default. A manager can still close any one")
    print("spec on their own bike.")
    print()
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
