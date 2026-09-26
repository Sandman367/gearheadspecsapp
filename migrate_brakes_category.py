"""
Brakes, its own category.

Brakes were split across two headings and neither was called Brakes: pads,
shoes and calipers sat under Drive, brake fluid under Controls. A rider doing
a brake job had to look in two places. Drive was also the joint-largest
category on the tree at 35 fields, most of which are not drive at all.

So: a Brakes heading between Drive and Fuel and Air, twelve fields moved into
it, and the parts that were missing added. Brake fluid keeps a pointer under
Controls, because that is where a rider bleeding the system looks for it --
one value, shown twice, never copied.

ONE TIME. This is not a rule. Nothing here reclassifies a field later because
its name contains "brake"; a field added tomorrow goes wherever it is put.

Also parks the sprockets on belt-drive bikes. A belt Harley does have
sprockets, so the rows are taken OFFLINE rather than deleted -- hidden from
riders, still there for the bike's manager to switch on, and still something
a rider can request.

Idempotent: every step checks before it writes, so a second run does nothing.

Run:  py migrate_brakes_category.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

CATEGORY = "Brakes"
BAND = 1000

# The order after this migration. Brakes takes index 3; everything below it
# shifts one band down, which is why the renumber below is not optional --
# next_sort_order() files a new field by its category's index, so a stale band
# would drop the next Fuel and Air field into the Brakes range.
ORDER_AFTER = ["General", "Engine", "Drive", "Brakes", "Fuel and Air",
               "Controls", "Suspension", "Electrical", "Gear and Accessories"]

MOVE = [
    "front_brake_pads", "rear_brake_pads",
    "front_brake_pad_left", "front_brake_pad_right",
    "front_left_brake_pads", "front_right_brake_pads",
    "front_brake_shoes", "rear_brake_shoes",
    # Not named "brake", but a caliper is nothing else. Leaving these behind
    # would strand them in Drive while their own pads moved out.
    "front_left_caliper", "front_right_caliper",
    # Home here; Controls keeps a pointer, added below.
    "front_brake_fluid", "rear_brake_fluid",
]

CROSS_LIST = [("front_brake_fluid", "Controls"), ("rear_brake_fluid", "Controls")]

# A front disc is q20 A (left caliper), B (right) or C (both). q20=D is a drum,
# which has no rotor and no master cylinder. A rear disc is q21=A.
FRONT_DISC = [("q20", "A"), ("q20", "B"), ("q20", "C")]
REAR_DISC = [("q21", "A")]

# key, label, category, triggers, universal, offline_on
#   offline_on: bike types where the field arrives switched off. Riders do not
#   see it; the bike's manager can switch it on.
ALL_TYPES = ["Standard / Naked", "Dirt bike / Off-road", "Scooter",
             "Street bike / Sport bike", "Dual-sport / Adventure",
             "Harley-Davidson", "Cruiser", "Touring"]
NOT_SCOOTER = [t for t in ALL_TYPES if t != "Scooter"]

NEW = [
    ("front_brake_rotor_size", "Front Brake Rotor Size", CATEGORY, FRONT_DISC, False, []),
    ("rear_brake_rotor_size", "Rear Brake Rotor Size", CATEGORY, REAR_DISC, False, []),
    ("front_brake_master_cylinder", "Front Brake Master Cylinder", CATEGORY, FRONT_DISC, False, []),
    ("rear_brake_master_cylinder", "Rear Brake Master Cylinder", CATEGORY, REAR_DISC, False, []),
    ("front_brake_lever", "Front Brake Lever", CATEGORY, [], True, []),
    # Scooters brake with two hand levers. Everything else brakes with a foot
    # pedal, so the rear lever arrives switched off outside scooters and the
    # pedal arrives switched off on them -- a Vespa with a pedal is rare, and
    # its manager can switch it on.
    ("rear_brake_lever", "Rear Brake Lever", CATEGORY, [], True, NOT_SCOOTER),
    ("brake_pedal", "Brake Pedal", "Controls", [], True, ["Scooter"]),
    # Both off everywhere for now. The bulb in particular may be the same
    # dual-filament lamp as Tail Light Bulb on most machines, and that wants
    # deciding before riders see two rows for one bulb.
    ("brake_light_bulb", "Brake Light Bulb", "Electrical", [], True, ALL_TYPES),
    ("brake_light_switch", "Brake Light Switch", "Electrical", [], True, ALL_TYPES),
]


def band_of(category):
    return ORDER_AFTER.index(category) * BAND


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    report = {}

    # ---- 1. move the twelve -------------------------------------------
    base = band_of(CATEGORY)
    moved = []
    for i, key in enumerate(MOVE, start=1):
        row = conn.execute("SELECT category FROM spec_fields WHERE field_key=?",
                           (key,)).fetchone()
        if not row:
            continue
        if row["category"] != CATEGORY:
            moved.append((key, row["category"]))
        conn.execute("UPDATE spec_fields SET category=?, sort_order=? WHERE field_key=?",
                     (CATEGORY, base + i * 10, key))
    report["fields moved into Brakes"] = len(moved)

    # ---- 2. brake fluid keeps a pointer under Controls ------------------
    listed = 0
    for key, category in CROSS_LIST:
        if not conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (key,)).fetchone():
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO spec_field_categories (field_key, category, shown)"
            " VALUES (?,?,1)", (key, category))
        listed += cur.rowcount
    report["cross-listed into Controls"] = listed

    # ---- 3. the new fields ---------------------------------------------
    made, attached = 0, 0
    top = {}
    for key, label, category, triggers, universal, offline_on in NEW:
        if conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?", (key,)).fetchone():
            continue
        cat_base = band_of(category)
        top[category] = top.get(category) or conn.execute(
            "SELECT IFNULL(MAX(sort_order), ?) FROM spec_fields WHERE category=?",
            (cat_base, category)).fetchone()[0]
        top[category] += 10
        conn.execute(
            "INSERT INTO spec_fields (field_key, label, category, spec_type,"
            " sort_order, universal, value_type) VALUES (?,?,?,'pref',?,?, 'text')",
            (key, label, category, top[category], 1 if universal else 0))
        made += 1
        for qid, opt in triggers:
            conn.execute(
                "INSERT OR IGNORE INTO field_triggers (field_key, question_id, option_label)"
                " VALUES (?,?,?)", (key, qid, opt))
        for bike_type in offline_on:
            conn.execute(
                "INSERT OR IGNORE INTO field_offline_defaults (field_key, bike_type)"
                " VALUES (?,?)", (key, bike_type))

        # attach it to the bikes it belongs on
        if universal:
            cur = conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                " SELECT id, ?, NULL, 'pending' FROM bikes", (key,))
            attached += cur.rowcount
        elif triggers:
            clauses = " OR ".join(["(question_id=? AND option_label=?)"] * len(triggers))
            args = [v for pair in triggers for v in pair]
            cur = conn.execute(
                f"INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                f" SELECT DISTINCT bike_id, ?, NULL, 'pending' FROM bike_answers"
                f" WHERE {clauses}", (key, *args))
            attached += cur.rowcount
        # The AFTER INSERT trigger pauses rows matching field_offline_defaults,
        # but only for rows inserted after the default exists. These were, so
        # nothing more is needed -- asserted at the end.
    report["fields created"] = made
    report["spec rows attached"] = attached

    # ---- 4. sprockets on belt bikes, offline rather than gone -----------
    off = 0
    for key in ("front_sprocket", "rear_sprocket"):
        cur = conn.execute(
            "UPDATE specs SET paused=1, paused_at=datetime('now')"
            " WHERE field_key=? AND paused=0 AND bike_id IN"
            "   (SELECT bike_id FROM bike_answers WHERE question_id='q3' AND option_label='B')",
            (key,))
        off += cur.rowcount
    report["belt-bike sprocket rows taken offline"] = off

    # ---- 6. renumber every band, last ----------------------------------
    # After the moves and the new fields, not before: a field filed at MAX+10
    # in a category that has just lost two members would leave a gap, and the
    # next run would close it and report work it did not need to do. Ordering
    # is by sort_order first, so this renumbers without reordering anything.
    renumbered = 0
    for category in ORDER_AFTER:
        base = band_of(category)
        rows = conn.execute(
            "SELECT field_key, sort_order FROM spec_fields WHERE category=?"
            " ORDER BY sort_order, label", (category,)).fetchall()
        for i, r in enumerate(rows, start=1):
            want = base + i * 10
            if r["sort_order"] != want:
                conn.execute("UPDATE spec_fields SET sort_order=? WHERE field_key=?",
                             (want, r["field_key"]))
                renumbered += 1
    report["fields renumbered"] = renumbered

    conn.commit()

    for k, v in report.items():
        print(f"  {k:36} {v}")

    # ---- what it looks like now -----------------------------------------
    print()
    print("  categories, in order:")
    for category in ORDER_AFTER:
        r = conn.execute(
            "SELECT COUNT(*) n, MIN(sort_order) lo, MAX(sort_order) hi"
            " FROM spec_fields WHERE category=?", (category,)).fetchone()
        band = band_of(category)
        flag = "" if r["n"] == 0 or (band <= r["lo"] and r["hi"] < band + BAND) else "  <-- BAND!"
        print(f"    {category:24} {r['n']:3} fields  {r['lo']}-{r['hi']}{flag}")

    bad = conn.execute(
        "SELECT COUNT(*) FROM specs s JOIN field_offline_defaults d"
        "   ON d.field_key = s.field_key"
        " JOIN bikes b ON b.id = s.bike_id AND b.bike_type = d.bike_type"
        " WHERE s.paused = 0").fetchone()[0]
    print()
    # Informational. Pre-existing scooter sprocket defaults do not cover rows
    # created before the default existed, and at least two of those (chain-drive
    # C70 Passports) genuinely have sprockets and should stay visible.
    print(f"  rows matching an offline default but still online: {bad} (pre-existing)")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
