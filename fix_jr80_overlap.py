"""
Two JR80 records, one overlapping the other.

The live site has "Suzuki JR80" (2001-2015, years confirmed by its manager)
and the catalogue's "Suzuki JR80 (minis)", which was 2018-2023 until
fix_vanvan_jr80_years.py stretched it to 2001-present -- written against a
copy of the database that did not have the first record, so it laid the
catalogue bike over the managed one. A search for a 2016 JR80 then landed
on the unmanaged copy.

This puts the catalogue record after the managed one: it starts the year
after "JR80" ends (2016), keeping "-present". The managed record is never
touched. Years moved off are dropped only if nobody has them in a garage.

Does nothing when there is no plain "JR80" record, or when the two no
longer overlap -- so it is safe on every start.

Run:  py fix_jr80_overlap.py [path/to/data.db]
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
    try:
        managed = conn.execute(
            "SELECT id, year_end FROM bikes WHERE make='Suzuki' AND model_code='JR80'").fetchone()
        other = conn.execute(
            "SELECT id, year_start FROM bikes WHERE make='Suzuki' AND model_code='JR80 (minis)'").fetchone()
        if not managed or not other or managed[1] is None or other[1] > managed[1]:
            print("JR80 records do not overlap; nothing to do")
            return
        start = managed[1] + 1
        conn.execute("UPDATE bikes SET year_start=? WHERE id=?", (start, other[0]))
        conn.execute(
            "DELETE FROM bike_years WHERE bike_id=? AND year<?"
            " AND id NOT IN (SELECT bike_year_id FROM user_bikes WHERE bike_year_id IS NOT NULL)",
            (other[0], start))
        conn.commit()
        print(f"JR80 (minis) (bike {other[0]}) now starts in {start}, after JR80 (bike {managed[0]})")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
