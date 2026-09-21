"""
Let an approved branch proposal join the questionnaire.

The hole this closes: approving a proposal created a row in spec_fields and
nothing else. build_spec_tree walks the static questionnaire.json, so a field
created at runtime could never be triggered by any future bike — it existed,
appeared in no tree, and quietly did nothing. The only way it reached a bike at
all was the "add it to the proposing bike" tick-box, one bike, once.

Two tables fix it:

  field_triggers  — "this field applies when Q3 is answered A". The static
                    questionnaire.json still defines the built-in tree; this is
                    how fields added later attach to the same branches.

  bike_answers    — what a bike actually answered. Not stored before, which
                    meant there was no record of WHY a bike had its fields, and
                    no way to find the bikes a newly attached field should
                    apply to.

Additive and idempotent.

Run:  py migrate_field_triggers.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS field_triggers (
         id           INTEGER PRIMARY KEY,
         field_key    TEXT    NOT NULL REFERENCES spec_fields(field_key) ON DELETE CASCADE,
         question_id  TEXT    NOT NULL,
         option_label TEXT    NOT NULL,
         created_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
         created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
         UNIQUE (field_key, question_id, option_label)
       )""",
    "CREATE INDEX IF NOT EXISTS idx_field_triggers_answer"
    " ON field_triggers (question_id, option_label)",

    # One answer per question per bike — re-running the questionnaire replaces
    # the answer rather than accumulating contradictory ones.
    """CREATE TABLE IF NOT EXISTS bike_answers (
         bike_id      INTEGER NOT NULL REFERENCES bikes(id) ON DELETE CASCADE,
         question_id  TEXT    NOT NULL,
         option_label TEXT    NOT NULL,
         answered_by  INTEGER REFERENCES users(id) ON DELETE SET NULL,
         answered_at  TEXT    NOT NULL DEFAULT (datetime('now')),
         PRIMARY KEY (bike_id, question_id)
       )""",
    "CREATE INDEX IF NOT EXISTS idx_bike_answers_answer"
    " ON bike_answers (question_id, option_label)",
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    def objects():
        return {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}

    before = objects()
    conn.execute("BEGIN")
    try:
        for sql in STATEMENTS:
            conn.execute(sql)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    added = sorted(objects() - before)
    print("added:", ", ".join(added) if added else "nothing (already migrated)")
    print("field_triggers rows:", conn.execute("SELECT COUNT(*) FROM field_triggers").fetchone()[0])
    print("bike_answers rows  :", conn.execute("SELECT COUNT(*) FROM bike_answers").fetchone()[0])
    print()
    print("Bikes with recorded questionnaire answers:",
          conn.execute("SELECT COUNT(DISTINCT bike_id) FROM bike_answers").fetchone()[0],
          "of", conn.execute("SELECT COUNT(*) FROM bikes").fetchone()[0])
    print("  (bulk-catalog bikes never ran the questionnaire, so a branch-attached")
    print("   field will not reach them — by design.)")
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
