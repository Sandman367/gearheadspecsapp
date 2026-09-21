"""
Let a bike's manager take a General field out of THEIR bike's header.

General fields sit in every bike's header by Spec Tree rule. Pinning was built
to add to that and never subtract from it, so a General field on a bike had no
way off the header at all -- the row said "in header" as a label, not a button.
A manager who wants Wheelbase out of the CB919's header, and only the CB919's,
had nothing to click.

bike_header_specs gains `hidden`. A row with hidden=1 on a General field means
"not in this bike's header"; the field keeps its section below. It is the same
table as pinning because it is the same kind of thing: one bike's manager
overriding the site-wide default for one field.

Additive and idempotent.

Run:  py migrate_header_hidden.py [path/to/data.db]
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(bike_header_specs)")}
    if "hidden" in cols:
        print("hidden already present")
    else:
        conn.execute("ALTER TABLE bike_header_specs ADD COLUMN hidden INTEGER"
                     " NOT NULL DEFAULT 0 CHECK (hidden IN (0,1))")
        conn.commit()
        print("hidden added")
    n = conn.execute("SELECT COUNT(*) FROM bike_header_specs WHERE hidden=1").fetchone()[0]
    print(f"General fields hidden from a header somewhere: {n}")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
