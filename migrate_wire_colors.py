"""
Make wire colours a value type instead of free text.

Four fields on the platform are wire colours. Typed as text they arrive as
"Yellow/Red", "yellow w/ red stripe", "Y/R" and "yel-red" from four riders, and
none of those match each other, sort together, or tell the page what to draw.

spec_fields gains `value_type`. Everything stays 'text'; these four become
'wire_color', which makes the bike page offer a colour picker instead of a text
box and render the value as a drawn wire plus the abbreviation the bike's own
manufacturer uses.

Values already entered are converted rather than discarded -- "Yellow/Red"
parses cleanly to "yellow/red". Anything that cannot be read as a colour pair is
reported and left exactly as it is: a value somebody sourced is worth more than
a tidy column, and a human should decide what it meant.

Factory Color is deliberately not included. It is the paint on the tank, not a
wire, and the vocabulary here is wire-jacket colours.

Additive and idempotent.

Run:  py migrate_wire_colors.py [path/to/data.db]
"""
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wire_colors

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

WIRE_FIELDS = [
    "starter_switch_wire_color",
    "left_turn_signal_wire_color",
    "right_turn_signal_wire_color",
    "kickstand_wire_color",
]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute("PRAGMA table_info(spec_fields)")}
    if "value_type" not in cols:
        conn.execute(
            "ALTER TABLE spec_fields ADD COLUMN value_type TEXT NOT NULL"
            " DEFAULT 'text'"
            " CHECK (value_type IN ('text','wire_color'))")
        print("added spec_fields.value_type")
    else:
        print("spec_fields.value_type already present")

    # Any field whose name says it is a wire colour, plus the known list, so a
    # field added since this was written is not silently left as text.
    found = [r["field_key"] for r in conn.execute(
        "SELECT field_key FROM spec_fields"
        " WHERE LOWER(label) LIKE '%wire%' AND LOWER(label) LIKE '%color%'")]
    targets = sorted(set(WIRE_FIELDS) | set(found))

    marked = 0
    for key in targets:
        row = conn.execute("SELECT label, value_type FROM spec_fields"
                           " WHERE field_key=?", (key,)).fetchone()
        if row is None:
            print(f"  no such field, skipping: {key}")
            continue
        if row["value_type"] == "wire_color":
            continue
        conn.execute("UPDATE spec_fields SET value_type='wire_color'"
                     " WHERE field_key=?", (key,))
        print(f"  {row['label']} is now a wire colour")
        marked += 1
    conn.commit()

    # Convert what riders already entered.
    converted, kept = 0, []
    for r in conn.execute(
            "SELECT s.id, s.value, f.label, s.bike_id FROM specs s"
            " JOIN spec_fields f ON f.field_key = s.field_key"
            " WHERE f.value_type='wire_color'"
            "   AND s.value IS NOT NULL AND TRIM(s.value) <> ''").fetchall():
        try:
            canon = wire_colors.normalise_set(r["value"])
        except wire_colors.WireColorError as e:
            kept.append((r["bike_id"], r["label"], r["value"], str(e)))
            continue
        if canon != r["value"]:
            conn.execute("UPDATE specs SET value=? WHERE id=?", (canon, r["id"]))
            print(f"  bike {r['bike_id']}: {r['value']!r} -> {canon!r}")
            converted += 1

    # Alternates are rider submissions on the same fields and need the same
    # treatment, or an alternate stays unreadable next to a converted stock.
    for r in conn.execute(
            "SELECT a.id, a.text, f.label FROM spec_alternates a"
            " JOIN specs s ON s.id = a.spec_id"
            " JOIN spec_fields f ON f.field_key = s.field_key"
            " WHERE f.value_type='wire_color'").fetchall():
        try:
            canon = wire_colors.normalise_set(r["text"])
        except wire_colors.WireColorError as e:
            kept.append(("alt", r["label"], r["text"], str(e)))
            continue
        if canon != r["text"]:
            conn.execute("UPDATE spec_alternates SET text=? WHERE id=?",
                         (canon, r["id"]))
            converted += 1
    conn.commit()

    print()
    print(f"fields marked   : {marked}")
    print(f"values converted: {converted}")
    if kept:
        print(f"left alone      : {len(kept)} — not readable as a wire colour")
        for bike, label, val, why in kept:
            print(f"    bike {bike} · {label}: {val!r}")
            print(f"      {why}")
        print("  These stay exactly as they are. Decide what each meant and")
        print("  re-enter it through the picker.")

    n = conn.execute("SELECT COUNT(*) FROM spec_fields"
                     " WHERE value_type='wire_color'").fetchone()[0]
    print(f"\nwire colour fields: {n}")
    for r in conn.execute("SELECT label FROM spec_fields"
                          " WHERE value_type='wire_color' ORDER BY sort_order"):
        print("   ", r["label"])
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
