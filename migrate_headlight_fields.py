"""
Register the fields the new headlight questions trigger.

Adding questions to data/questionnaire.json is only half of adding a question.
The seeder registers every triggerable field up front, and build_spec_tree
refuses to invent one it has not seen:

    raise HttpError(500, f"questionnaire triggered unregistered field {label!r}")

That refusal is deliberate — silently creating the field would hide a drift
between the questionnaire and the registry — but it means a live database needs
the new fields added explicitly, or the first rider to answer "yes, one
headlight" gets a 500 instead of a spec sheet.

The four fields, all Electrical:

    Head Light Bulb     one headlight, or two that are each high and low
    High Beam Bulb      two headlights with the beams split between them
    Low Beam Bulb       "
    Parking Light Bulb  independent of how many headlights there are

"Head Light Bulb" is spelled the way the pending branch proposal spells it,
rather than "Headlight Bulb", so the two do not end up as separate fields for
the same object. Once this has run, that proposal is redundant and should be
rejected — approving it would be refused as a duplicate anyway.

Sort order places them among the existing Electrical fields rather than at the
end of the band: bulbs belong next to the turn signal bulb, not after the
wiring colours.

Additive and idempotent.

Run:  py migrate_headlight_fields.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

CATEGORY = "Electrical"
NEW_FIELDS = [
    ("head_light_bulb",    "Head Light Bulb"),
    ("high_beam_bulb",     "High Beam Bulb"),
    ("low_beam_bulb",      "Low Beam Bulb"),
    ("parking_light_bulb", "Parking Light Bulb"),
]
# The four sit directly after this one, which is the existing bulb field.
ANCHOR = "turn_signal_light_bulb"


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    anchor = conn.execute(
        "SELECT sort_order FROM spec_fields WHERE field_key=?", (ANCHOR,)).fetchone()
    if anchor:
        start = anchor["sort_order"]
    else:
        # No anchor: fall back to the end of the category's band.
        start = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) FROM spec_fields WHERE category=?",
            (CATEGORY,)).fetchone()[0]
        print(f"note: {ANCHOR} not found, appending to the end of {CATEGORY}")

    # Make room: everything below the anchor shifts down by the number of new
    # fields, so the four land in a contiguous run without colliding.
    todo = [(k, l) for k, l in NEW_FIELDS
            if not conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                                (k,)).fetchone()]
    if not todo:
        print("all four fields already registered — nothing to do")
    else:
        shift = len(todo) * 10
        conn.execute(
            "UPDATE spec_fields SET sort_order = sort_order + ?"
            " WHERE category = ? AND sort_order > ?", (shift, CATEGORY, start))
        for i, (key, label) in enumerate(todo, start=1):
            conn.execute(
                "INSERT INTO spec_fields (field_key, label, category, spec_type,"
                " sort_order) VALUES (?,?,?,'pref',?)",
                (key, label, CATEGORY, start + i * 10))
            print(f"  registered {label!r} at {start + i * 10}")
        conn.commit()

    lo, hi, n = conn.execute(
        "SELECT MIN(sort_order), MAX(sort_order), COUNT(*) FROM spec_fields"
        " WHERE category=?", (CATEGORY,)).fetchone()
    band_lo = (start // 1000) * 1000
    print(f"{CATEGORY}: {n} fields, sort_order {lo}..{hi}")
    if hi >= band_lo + 1000:
        print("  WARNING: the category has overflowed its 1000-wide band")

    print("\nElectrical, in order:")
    for r in conn.execute(
            "SELECT sort_order, label FROM spec_fields WHERE category=?"
            " ORDER BY sort_order", (CATEGORY,)):
        print(f"  {r['sort_order']:6}  {r['label']}")

    print("\nfk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
