"""
Rewrite the sibling-divergence view so it starts from split lineages, and
switch the database to WAL.

The original view joined every spec row against every other spec of the same
field to find siblings -- 108,000 specs x 2,000 rows per field once the model
lists went in, a query that runs for minutes. The admin dashboard asks for it
on every load, each visit left a thread grinding on it, and a thread reading
holds a shared lock in rollback-journal mode: every write on the site then
waited five seconds and failed with "database is locked".

Two changes, both idempotent:

  * sibling_divergence now begins with the bikes that ARE in a split lineage
    (a split_from_bike_id, or the bike one points at), pairs them, and only
    then looks at their specs. Nothing split yet: nothing to scan.
  * journal_mode=WAL, so a reader never blocks a writer again even if some
    other query turns out slow. WAL keeps recent commits in data.db-wal until
    a checkpoint, so a backup must be taken with the SQLite backup API (or
    after PRAGMA wal_checkpoint(TRUNCATE)) rather than by copying data.db
    alone; the import scripts do that.

Run:  py migrate_divergence_view.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

SIBLING_DIVERGENCE = """
CREATE VIEW sibling_divergence AS
WITH lineage AS (
  SELECT id, COALESCE(split_from_bike_id, id) AS root FROM bikes
  WHERE split_from_bike_id IS NOT NULL
     OR id IN (SELECT split_from_bike_id FROM bikes WHERE split_from_bike_id IS NOT NULL)
),
pairs AS (
  SELECT la.root, la.id AS bike_a, lb.id AS bike_b
  FROM lineage la
  JOIN lineage lb ON lb.root = la.root AND la.id < lb.id
)
SELECT
  p.root    AS lineage_root,
  p.bike_a,
  p.bike_b,
  a.field_key,
  a.value   AS value_a,
  b.value   AS value_b
FROM pairs p
JOIN specs a ON a.bike_id = p.bike_a
JOIN specs b ON b.bike_id = p.bike_b AND b.field_key = a.field_key
WHERE IFNULL(a.value,'') <> IFNULL(b.value,'');
"""

DIVERGENCE_QUEUE = """
CREATE VIEW divergence_queue AS
SELECT d.*
FROM sibling_divergence d
WHERE NOT EXISTS (
  SELECT 1 FROM divergence_ack a
  WHERE a.bike_a = d.bike_a
    AND a.bike_b = d.bike_b
    AND a.field_key = d.field_key
    AND IFNULL(a.value_a,'') = IFNULL(d.value_a,'')
    AND IFNULL(a.value_b,'') = IFNULL(d.value_b,'')
);
"""


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.executescript(
        "DROP VIEW IF EXISTS divergence_queue;"
        "DROP VIEW IF EXISTS sibling_divergence;"
        + SIBLING_DIVERGENCE + DIVERGENCE_QUEUE)
    conn.commit()
    mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
    n = conn.execute("SELECT COUNT(*) FROM divergence_queue").fetchone()[0]
    conn.close()
    print(f"views rewritten; journal_mode={mode}; divergence queue: {n}")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
