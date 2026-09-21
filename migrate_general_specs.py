"""
Fix and fill out the General category, which now drives the bike hero.

Two things:

1. "Curb Weight" was mislabelled. Its value is "193.7 kg / 427 lb (dry)" — a
   DRY weight, which excludes fluids, while curb weight includes them. The
   label contradicted the value it was attached to. Only the label changes;
   field_key stays `curb_weight` because it is referenced by existing specs
   rows, and the schema exists precisely so a rename is one UPDATE rather than
   a sweep. The stale key is invisible to readers.

2. General lacked Rake, Trail and Factory Color, so the at-a-glance block had
   almost nothing to show. Values come from the same 2005 Honda press specs
   the rest of the CB919 sheet is sourced from, so they carry confidence 'mfr'.
   Fuel Tank Capacity already existed but was empty; it is filled here and
   deliberately left in Fuel and Air, where it belongs.

Additive and idempotent apart from the rename, which is a no-op once applied.

Run:  py migrate_general_specs.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

# label, field_key, sort nudge, value for the CB919
NEW_GENERAL = [
    ("Rake (Caster Angle)", "rake_caster_angle", "25.0°"),
    ("Trail",               "trail",             "3.9 in (98 mm)"),
    ("Factory Color",       "factory_color",     "Metallic Black"),
]

CB919_MODEL = "CB900F2 919"


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    cb = conn.execute("SELECT id FROM bikes WHERE model_code=?", (CB919_MODEL,)).fetchone()
    cb919 = cb[0] if cb else None

    conn.execute("BEGIN")
    try:
        # 1. the mislabel
        renamed = conn.execute(
            "UPDATE spec_fields SET label='Dry Weight'"
            " WHERE field_key='curb_weight' AND label <> 'Dry Weight'").rowcount

        # 2. the missing General fields
        order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM spec_fields WHERE category='General'"
        ).fetchone()[0]

        added_fields = added_values = 0
        for i, (label, key, value) in enumerate(NEW_GENERAL, start=1):
            cur = conn.execute(
                "INSERT OR IGNORE INTO spec_fields"
                " (field_key, label, category, spec_type, sort_order)"
                " VALUES (?,?,'General','fixed',?)", (key, label, order + i))
            added_fields += cur.rowcount
            if cb919:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                    " VALUES (?,?,?,'mfr')", (cb919, key, value))
                added_values += cur.rowcount

        # 3. a value for the fuel capacity field that already existed empty
        filled = 0
        if cb919:
            filled = conn.execute(
                "UPDATE specs SET value=?, confidence='mfr'"
                " WHERE bike_id=? AND field_key='fuel_tank_capacity'"
                "   AND (value IS NULL OR value='')",
                ("5.0 gal (18.9 L)", cb919)).rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    print(f"label fixed        : {'Curb Weight -> Dry Weight' if renamed else 'already Dry Weight'}")
    print(f"General fields added: {added_fields}")
    print(f"CB919 values added : {added_values}")
    print(f"fuel capacity filled: {filled}")
    print()
    print("CB919 General now:")
    for r in conn.execute(
            "SELECT f.label, s.value FROM specs s"
            " JOIN spec_fields f ON f.field_key = s.field_key"
            " WHERE s.bike_id=? AND f.category='General'"
            " ORDER BY f.sort_order, f.label", (cb919,)):
        print(f"   {r[0]:22} {r[1] or '— not yet sourced —'}")

    print("\nfk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
