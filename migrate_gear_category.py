"""
A "Gear and Accessories" category, last on every spec sheet.

Helmet, gloves, boots, goggles, heated vest, jacket: what riders wear on this
bike, not what the factory fitted. So the fields are `community` (no
manufacturer value exists -- values come from riders and are voted on),
universal (every bike, whatever the questionnaire said), and plain text.
They sit in the band after Electrical, so the heading comes last. Idempotent.

Run:  py migrate_gear_category.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

CATEGORY = "Gear and Accessories"
BAND_INDEX = 7                        # after General .. Electrical
FIELDS = [("helmet", "Helmet"), ("gloves", "Gloves"), ("boots", "Boots"),
          ("goggles", "Goggles"), ("heated_vest", "Heated Vest"), ("jacket", "Jacket")]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    added = 0
    for i, (key, label) in enumerate(FIELDS, start=1):
        cur = conn.execute(
            "INSERT OR IGNORE INTO spec_fields (field_key, label, category, spec_type,"
            " sort_order, universal, value_type) VALUES (?,?,?,'community',?,1,'text')",
            (key, label, CATEGORY, BAND_INDEX * 1000 + i * 10))
        added += cur.rowcount
    rows = conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " SELECT b.id, f.field_key, NULL, 'pending' FROM bikes b"
        " JOIN spec_fields f ON f.category=? AND f.universal=1", (CATEGORY,)).rowcount
    conn.commit()
    conn.close()
    print(f"{CATEGORY}: {added} field(s) added, {rows} spec row(s) put on bikes")


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
