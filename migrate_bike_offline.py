"""
A whole bike can be taken offline: riders don't see it in Browse, search, the
counts or at its address; admin still does, and can put it back.

The first time the column is added, the fully electric bikes go offline --
the Spec Tree has no electric specs yet, so their sheets are empty
universals. That step runs only once, when the column is created, so a bike
admin later puts back online stays online across restarts. Idempotent.

Run:  py migrate_bike_offline.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

# (make, model_code) of every fully electric bike in the catalogue. The
# Kawasaki Ninja 7 Hybrid is left out: it has a petrol engine.
ELECTRIC = [
    ("GasGas", "TXE"), ("GasGas", "MC-E"), ("Husqvarna", "EE"),
    ("KTM", "Freeride E"), ("KTM", "SX-E"), ("Piaggio", "Piaggio 1"),
    ("Victory", "Empulse TT"), ("Vespa", "Elettrica"),
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(bikes)")]
    if "offline" in cols:
        print("bikes.offline already in place")
    else:
        conn.execute("ALTER TABLE bikes ADD COLUMN offline INTEGER NOT NULL DEFAULT 0"
                     " CHECK (offline IN (0,1))")
        n = 0
        for make, code in ELECTRIC:
            n += conn.execute("UPDATE bikes SET offline=1 WHERE make=? AND model_code=?",
                              (make, code)).rowcount
        conn.commit()
        print(f"bikes.offline added; {n} electric bike(s) taken offline")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
