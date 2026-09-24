"""
Where a value came from, when nobody typed it.

5,452 of the values on this site have no author. They were not entered by
a rider -- they were seeded from published model lists when the catalogue
was built: engine displacement, cylinder configuration, drive type, tyre
sizes, batteries, sprockets. The page showed nothing at all beside them,
because the attribution line only renders when there is a username, so a
reader had no way to tell those apart from a spec somebody had sourced.

That is the site claiming less than it should and more than it should at
the same time: no source shown, while the page says every value shows who
entered it.

specs.value_source records the answer for those rows. 'catalogue' means it
came from the list the catalogue was seeded from -- not from a manual, not
from a rider. A value a person writes clears it, because from that moment
it is theirs.

Idempotent: the second run has nothing to backfill.

Run:  py migrate_value_source.py [path/to/data.db]
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
    cols = {r[1] for r in conn.execute("PRAGMA table_info(specs)")}
    if "value_source" not in cols:
        conn.execute("ALTER TABLE specs ADD COLUMN value_source TEXT")

    # A value with no author got there by an import, and every import this
    # database has seen was the catalogue seed. A row with an author is left
    # alone: the username is the better answer and this column stays empty.
    cur = conn.execute(
        "UPDATE specs SET value_source='catalogue'"
        " WHERE value IS NOT NULL AND value <> '' AND entered_by IS NULL"
        "   AND value_source IS NULL")
    conn.commit()

    total = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE value IS NOT NULL AND value <> ''").fetchone()[0]
    marked = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE value_source='catalogue'").fetchone()[0]
    orphan = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE value IS NOT NULL AND value <> ''"
        "   AND entered_by IS NULL AND value_source IS NULL").fetchone()[0]
    print(f"marked as catalogue-seeded this run: {cur.rowcount}")
    print(f"values on the site: {total}  ·  seeded: {marked}  ·  typed by a person: {total - marked}")
    if orphan:
        print(f"WARNING: {orphan} value(s) still have neither an author nor a source")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
