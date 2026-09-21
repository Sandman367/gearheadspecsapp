"""
Let one spec carry different values for different model years.

A bike whose tank grew in 1978 is still the same bike -- same engine, same
frame. Splitting it in two duplicates the twenty-eight specs that did not
change in order to vary the two that did, and then leans on the
sibling-divergence report to notice when those twenty-eight drift apart.
Splitting the SPEC keeps the duplication to the thing that actually differs.

Splitting the BIKE stays the right answer when the machine changed -- different
engine, different frame -- and the specs genuinely should not share a page.

specs gains year_from / year_to. Both NULL means "every year this bike covers",
which is what every existing row is, so nothing changes for data already
entered. The old UNIQUE(bike_id, field_key) has to go, because a field with
variants is several rows; it is replaced by two partial indexes that keep the
real rules:

    one unranged row per field, and one variant per start year

SQLite cannot drop a constraint, so the table is rebuilt. Row ids are preserved
so the four tables that point at specs.id -- alternates, votes, requests and
flags -- stay attached to exactly the rows they were on.

Idempotent: re-running finds the columns already present and stops.

Run:  py migrate_spec_years.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

NEW_TABLE = """
CREATE TABLE specs_new (
  id         INTEGER PRIMARY KEY,
  bike_id    INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
  field_key  TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
  value      TEXT,
  confidence TEXT    CHECK (confidence IN ('confirmed','mfr','pending')),
  spec_type  TEXT    CHECK (spec_type IS NULL OR spec_type IN ('fixed','pref','community')),
  tools      TEXT,
  paused     INTEGER NOT NULL DEFAULT 0 CHECK (paused IN (0,1)),
  paused_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
  paused_at  TEXT,
  entered_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
  updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
  created_at TEXT    NOT NULL DEFAULT (datetime('now')),
  year_from  INTEGER,
  year_to    INTEGER,
  CHECK ((year_from IS NULL) = (year_to IS NULL)),
  CHECK (year_from IS NULL OR year_from <= year_to)
)
"""

INDEXES = [
    "CREATE INDEX idx_specs_bike  ON specs (bike_id)",
    "CREATE INDEX idx_specs_field ON specs (field_key)",
    "CREATE UNIQUE INDEX idx_specs_one_unranged ON specs (bike_id, field_key)"
    " WHERE year_from IS NULL",
    "CREATE UNIQUE INDEX idx_specs_one_per_year ON specs (bike_id, field_key, year_from)"
    " WHERE year_from IS NOT NULL",
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute("PRAGMA table_info(specs)")}
    if "year_from" in cols:
        print("specs.year_from already present — checking the guard trigger")
        _guard(conn)
        _report(conn)
        conn.close()
        return

    before = conn.execute("SELECT COUNT(*) FROM specs").fetchone()[0]
    dependents = {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("spec_alternates", "spec_votes", "spec_requests", "value_flags")
    }
    print(f"specs before: {before}")
    for t, n in dependents.items():
        print(f"  {t}: {n} rows pointing at a spec")

    # Views that read specs have to stand aside for the rename and be put back
    # afterwards. Their definitions are taken from the database rather than
    # retyped here, so a view added since this was written survives.
    # Every view, not just the ones naming specs: views build on views here --
    # divergence_queue reads sibling_divergence -- so dropping one breaks the
    # next. sqlite_master lists them in creation order, which is an order they
    # can be put back in.
    views = [(r["name"], r["sql"]) for r in conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='view' AND sql IS NOT NULL")]
    print(f"views to rebuild: {', '.join(n for n, _ in views) or 'none'}")

    # Foreign keys off for the swap only. The dependents reference specs.id and
    # the ids are carried over unchanged, so nothing is actually orphaned --
    # but SQLite would refuse the DROP while they point at it.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("BEGIN")
    try:
        for name, _ in reversed(views):
            conn.execute(f"DROP VIEW IF EXISTS {name}")
        conn.execute(NEW_TABLE)
        conn.execute(
            "INSERT INTO specs_new (id, bike_id, field_key, value, confidence,"
            " spec_type, tools, paused, paused_by, paused_at, entered_by,"
            " updated_at, created_at, year_from, year_to)"
            " SELECT id, bike_id, field_key, value, confidence, spec_type,"
            " tools, paused, paused_by, paused_at, entered_by, updated_at,"
            " created_at, NULL, NULL FROM specs")
        conn.execute("DROP TABLE specs")
        conn.execute("ALTER TABLE specs_new RENAME TO specs")
        for sql in INDEXES:
            conn.execute(sql)
        for name, sql in views:
            # year_specs is the one view that has to change rather than come
            # back as it was: it expands a spec across every model year of the
            # bike, which is only right for a spec that covers every year.
            if name == "year_specs":
                sql = sql.replace(
                    "JOIN specs s       ON s.bike_id = b.id",
                    "JOIN specs s       ON s.bike_id = b.id"
                    " AND (s.year_from IS NULL"
                    " OR y.year BETWEEN s.year_from AND s.year_to)")
            conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        conn.execute("PRAGMA foreign_keys = ON")
        raise
    conn.execute("PRAGMA foreign_keys = ON")

    after = conn.execute("SELECT COUNT(*) FROM specs").fetchone()[0]
    if after != before:
        raise SystemExit(f"row count changed: {before} -> {after}; aborting")

    # Every dependent must still find its spec. This is the check that matters:
    # a rebuild that quietly orphaned votes would be worse than no rebuild.
    orphans = {}
    for t in dependents:
        col = "spec_id"
        orphans[t] = conn.execute(
            f"SELECT COUNT(*) FROM {t} d"
            f" LEFT JOIN specs s ON s.id = d.{col}"
            f" WHERE s.id IS NULL").fetchone()[0]
    conn.commit()

    print(f"specs after : {after}")
    for t, n in orphans.items():
        print(f"  {t}: {n} orphaned")
    if any(orphans.values()):
        raise SystemExit("rows were orphaned by the rebuild — restore the backup")

    _guard(conn)
    _report(conn)
    conn.close()


def _guard(conn):
    """Refuse an unranged row for a field that already has year variants.

    Everything that adds fields in bulk says INSERT OR IGNORE, meaning "only if
    it is missing". Once a field is split there is no unranged row to collide
    with, so those inserts would add a third row covering every year beside the
    two that split it. RAISE(IGNORE) is the same "skip it" those callers
    already expect, applied in one place rather than twelve.
    """
    conn.execute('''CREATE TRIGGER IF NOT EXISTS specs_no_unranged_beside_variants
BEFORE INSERT ON specs
WHEN NEW.year_from IS NULL
 AND EXISTS (SELECT 1 FROM specs
              WHERE bike_id = NEW.bike_id AND field_key = NEW.field_key)
BEGIN
  SELECT RAISE(IGNORE);
END''')
    conn.commit()
    n = len(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
        " AND name='specs_no_unranged_beside_variants'").fetchall())
    print("guard trigger:", "present" if n else "MISSING")


def _report(conn):
    fk = conn.execute("PRAGMA foreign_key_check").fetchall()
    print("fk_check :", fk or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    n = conn.execute(
        "SELECT COUNT(*) FROM specs WHERE year_from IS NOT NULL").fetchone()[0]
    print(f"year-scoped specs: {n} (everything else applies to every year)")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
