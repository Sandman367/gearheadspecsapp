"""
Renumber sort_order so the display follows the order somebody chose.

Every field in a category shared one sort_order, so the browse page fell
through to its tiebreaker — alphabetical by label. That split the two coolant
entries away from each other, sorted "Spark Plug Gap" above "Spark Plug —
Standard", and separated the oil specs from the oil change interval. Nobody
picked that order; it is what a tiebreaker produces when the real order has
been discarded.

The real order is in data/cb919_specs.json, which lists its specs the way a
mechanic reads them. This applies that to the live database without a re-seed.

Numbering is sparse (10, 20, 30) inside 1000-wide category bands:

  * sparse so inserting between two fields is one UPDATE, not a renumber;
  * banded so a category's fields stay contiguous — the browse page groups by
    consecutive runs, and interleaving two categories would render a duplicate
    category heading.

Fields the curated sheet does not mention (catalog columns, questionnaire-only
fields) keep their relative alphabetical order and follow the hinted ones.

Idempotent: running it twice produces the same numbers.

Run:  py migrate_field_order.py [path/to/data.db]
"""
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

CATEGORY_ORDER = ["General", "Engine", "Drive", "Fuel and Air",
                  "Controls", "Suspension", "Electrical"]


def slugify(label):
    s = label.lower().replace("×", " x ").replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")

    with open(os.path.join(ROOT, "data", "cb919_specs.json"), encoding="utf-8") as f:
        curated = json.load(f)["specs"]
    # field_key -> position in the curated sheet
    hints = {slugify(item["label"]): i for i, item in enumerate(curated)}

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    fields = conn.execute(
        "SELECT field_key, label, category, sort_order FROM spec_fields").fetchall()
    by_category = defaultdict(list)
    for key, label, category, current in fields:
        by_category[category].append((key, label, current))

    conn.execute("BEGIN")
    try:
        changed = 0
        for category, items in by_category.items():
            rank = CATEGORY_ORDER.index(category) if category in CATEGORY_ORDER else 99
            # Hinted fields in sheet order, then everything else alphabetically.
            items.sort(key=lambda t: (t[0] not in hints, hints.get(t[0], 0), t[1]))
            for i, (key, _label, current) in enumerate(items, start=1):
                new = rank * 1000 + i * 10
                if new != current:
                    conn.execute("UPDATE spec_fields SET sort_order=? WHERE field_key=?",
                                 (new, key))
                    changed += 1
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"renumbered: {changed} of {len(fields)} fields")
    print()
    print("Engine, as it will now display:")
    for i, r in enumerate(conn.execute(
            "SELECT label, sort_order FROM spec_fields WHERE category='Engine'"
            " ORDER BY sort_order LIMIT 17"), 1):
        print(f"  {i:2}. {r[0]:38} {r[1]}")

    # Each category must stay contiguous or the page renders its heading twice.
    prev_max, overlap = -1, []
    for cat in sorted({f[2] for f in fields},
                      key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99):
        lo, hi = conn.execute(
            "SELECT MIN(sort_order), MAX(sort_order) FROM spec_fields WHERE category=?",
            (cat,)).fetchone()
        if lo is not None and lo <= prev_max:
            overlap.append(cat)
        prev_max = max(prev_max, hi if hi is not None else -1)
    print()
    print("category bands overlap:", overlap or "none — each stays contiguous")
    conn.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
