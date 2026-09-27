"""
'fuse' as a value type.

A fuse is a size, a rating and what it protects -- three facts that belong
together, and that this site had as three separate text fields. Recorded
together the colour comes free, because blade fuses are colour coded by
rating under ISO 8820.

Widening the value_type CHECK on two tables. SQLite cannot alter a CHECK, so
each table is rebuilt from its own stored DDL with the new list substituted;
every row, index and default comes across unchanged. Idempotent: a table that
already allows 'fuse' is left alone.

Run:  py migrate_fuse_value_type.py [path/to/data.db]
"""
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

# The stored DDL is not always spelled the way schema.sql spells it: a table
# an earlier migration rebuilt comes back with spaces after the commas. So the
# list is found by pattern and rewritten in place, rather than matched as a
# fixed string that only happens to be right today.
CHECK_RE = re.compile(
    r"CHECK\s*\(\s*value_type\s+IN\s*\((?P<list>[^)]*'ethanol'\s*)\)\s*\)", re.I)

# Both tables that carry a value type: the Spec Tree's fields, and the
# proposals queue, where a rider naming a missing spec says what kind of
# value it holds.
TABLES = ("spec_fields", "branch_proposals")


def rebuild(conn, table):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    if not row:
        return f"{table}: not in this database"
    ddl = row[0]
    if "'fuse'" in ddl:
        return f"{table}: already allows 'fuse'"
    m = CHECK_RE.search(ddl)
    if not m:
        return f"{table}: CHECK not in the expected form, left alone"

    tmp = f"{table}__fuse_tmp"
    # The stored DDL may quote the table name; match either way.
    head = re.sub(r'CREATE TABLE ("?)' + re.escape(table) + r'\1',
                  f'CREATE TABLE "{tmp}"', ddl, count=1)
    # The CHECK moved if the name was requoted, so it is located again.
    m2 = CHECK_RE.search(head)
    new_ddl = (head[:m2.start()]
               + "CHECK (value_type IN (" + m2.group("list").rstrip() + ", 'fuse'))"
               + head[m2.end():])

    idx = [r[0] for r in conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=?"
        "   AND sql IS NOT NULL", (table,))]
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    names = ", ".join(f'"{c}"' for c in cols)

    conn.execute(new_ddl)
    conn.execute(f'INSERT INTO "{tmp}" ({names}) SELECT {names} FROM "{table}"')
    conn.execute(f'DROP TABLE "{table}"')
    conn.execute(f'ALTER TABLE "{tmp}" RENAME TO "{table}"')
    for sql in idx:
        conn.execute(sql)
    return f"{table}: rebuilt, 'fuse' allowed"


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    # Foreign keys OFF for the swap: dropping the old table would otherwise
    # cascade to every spec that points at it. legacy_alter_table keeps the
    # references pointing at the new table under the same name.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("PRAGMA legacy_alter_table = ON")
    before = {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")}
    conn.execute("BEGIN")
    try:
        out = [rebuild(conn, t) for t in TABLES]
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    after = {tuple(r) for r in conn.execute("PRAGMA foreign_key_check")}
    conn.execute("PRAGMA foreign_keys = ON")
    conn.close()

    for line in out:
        print(" ", line)

    # Only damage THIS migration caused is a failure. The database already
    # carries orphan sessions and password resets pointing at deleted users;
    # refusing to run because of those would mean it can never run, and
    # clearing them is somebody's decision rather than a side effect of
    # adding a value type.
    caused = after - before
    if caused:
        raise SystemExit(f"the rebuild broke foreign keys: {sorted(caused)[:5]}")
    if before:
        print(f"  note: {len(before)} pre-existing foreign key orphan(s), untouched")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
