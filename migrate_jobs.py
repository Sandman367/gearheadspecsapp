"""
Jobs: the work you do on a bike -- an oil change, a chain, brake pads -- as
one card on the bike page with every spec the job needs, the tools for it,
and the videos and threads riders found useful.

A job IS a service task. The service log already had them ("Engine oil &
filter", "Chain slack & lube", ...) with the specs each one shows, and the
interval it reads off the bike. This adds what a card needs on top: an icon,
a line of description, whether it is shown on bike pages yet, and per-bike
tools and links that belong to the job rather than to one spec (the drain
pan is for the oil change, not for "Engine Oil Volume").

The first time it runs it publishes the oil change only: renamed "Oil
change", given the rest of the oil specs, and started off with the tools and
links riders had already put on its specs (copied, votes and all -- the
per-spec ones stay where they were). The other tasks stay in the service log
and off bike pages until admin publishes them. Idempotent.

Run:  py migrate_jobs.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

TABLES = """
-- Per-bike tools for a job: a checklist anyone can add to, no voting.
CREATE TABLE IF NOT EXISTS job_tools (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  task_key   TEXT    NOT NULL REFERENCES service_tasks(task_key) ON DELETE CASCADE,
  text       TEXT    NOT NULL,
  added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, task_key, text)
);
-- Per-bike videos and threads for a job, voted so the useful ones rise.
CREATE TABLE IF NOT EXISTS job_links (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  task_key   TEXT    NOT NULL REFERENCES service_tasks(task_key) ON DELETE CASCADE,
  link_type  TEXT    NOT NULL CHECK (link_type IN ('yt','forum','doc','other')),
  title      TEXT    NOT NULL,
  url        TEXT    NOT NULL,
  added_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE (bike_id, task_key, url)
);
CREATE TABLE IF NOT EXISTS job_link_votes (
  link_id    INTEGER NOT NULL REFERENCES job_links(id) ON DELETE CASCADE,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (link_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_job_tools_target ON job_tools (bike_id, task_key);
CREATE INDEX IF NOT EXISTS idx_job_links_target ON job_links (bike_id, task_key);
"""

OIL_SPECS = ["engine_oil_weight", "engine_oil_volume", "oil_filter_part_number",
             "oil_filter_1_part_number", "oil_filter_2_part_number",
             "oil_screen_location", "oil_change_interval"]


def apply_defaults(conn):
    """Publish the oil change and give it its full set of specs, tools and
    links. Called once by migrate() and by seed.py, so a fresh database and an
    upgraded one start the same."""
    if not conn.execute("SELECT 1 FROM service_tasks WHERE task_key='oil'").fetchone():
        return
    conn.execute(
        "UPDATE service_tasks SET published=1, icon='oil',"
        " name=CASE WHEN name='Engine oil & filter' THEN 'Oil change' ELSE name END,"
        " description=COALESCE(description,"
        "   'Drain the old oil, change the filter, refill to the right level.')"
        " WHERE task_key='oil'")
    have = {r[0] for r in conn.execute("SELECT field_key FROM spec_fields")}
    for i, key in enumerate(OIL_SPECS):
        if key in have:
            conn.execute("INSERT OR IGNORE INTO service_task_specs (task_key, field_key, sort_order)"
                         " VALUES ('oil', ?, ?)", (key, i))
            conn.execute("UPDATE service_task_specs SET sort_order=? WHERE task_key='oil' AND field_key=?",
                         (i, key))
    # What riders had already attached to the oil specs starts the card off.
    marks = ",".join("?" * len(OIL_SPECS))
    conn.execute(
        f"INSERT OR IGNORE INTO job_tools (bike_id, task_key, text, added_by, paused, created_at)"
        f" SELECT bike_id, 'oil', text, added_by, paused, created_at FROM spec_tools"
        f" WHERE field_key IN ({marks}) ORDER BY id", OIL_SPECS)
    for link in conn.execute(
            f"SELECT id, bike_id, link_type, title, url, added_by, paused, created_at FROM spec_links"
            f" WHERE field_key IN ({marks}) ORDER BY id", OIL_SPECS).fetchall():
        cur = conn.execute(
            "INSERT OR IGNORE INTO job_links (bike_id, task_key, link_type, title, url, added_by, paused, created_at)"
            " VALUES (?, 'oil', ?,?,?,?,?,?)", link[1:])
        if cur.rowcount:
            conn.execute("INSERT OR IGNORE INTO job_link_votes (link_id, user_id, created_at)"
                         " SELECT ?, user_id, created_at FROM link_votes WHERE link_id=?",
                         (cur.lastrowid, link[0]))


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.executescript(TABLES)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(service_tasks)")]
        first = "published" not in cols
        if "icon" not in cols:
            conn.execute("ALTER TABLE service_tasks ADD COLUMN icon TEXT")
        if "description" not in cols:
            conn.execute("ALTER TABLE service_tasks ADD COLUMN description TEXT")
        if first:
            conn.execute("ALTER TABLE service_tasks ADD COLUMN published INTEGER NOT NULL DEFAULT 0"
                         " CHECK (published IN (0,1))")
            apply_defaults(conn)
        conn.commit()
        print("jobs in place" + ("; the oil change is published" if first else ""))
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
