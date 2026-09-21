"""
A proposed field carries what its values will be.

branch_proposals gains `value_type` ('text' by default, or wire_color /
fuel_octane / ethanol), chosen by whoever proposes the field -- a manager on
their dashboard, a rider on the request page -- and offered to admin as the
preselected choice on approval, where it can still be changed. Idempotent.

Run:  py migrate_proposal_value_type.py [path/to/data.db]
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
    cols = [r[1] for r in conn.execute("PRAGMA table_info(branch_proposals)")]
    if "value_type" in cols:
        print("branch_proposals.value_type already present")
    else:
        conn.execute(
            "ALTER TABLE branch_proposals ADD COLUMN value_type TEXT NOT NULL DEFAULT 'text'"
            " CHECK (value_type IN ('text','wire_color','fuel_octane','ethanol'))")
        conn.commit()
        print("added branch_proposals.value_type")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
