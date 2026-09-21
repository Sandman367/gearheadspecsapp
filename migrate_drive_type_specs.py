"""
Make each bike's drive fields agree with its own Drive Type.

The bulk catalog import wrote one spec row per mapped column for every bike,
unconditionally. Its own comment describes the intent —

    a missing row means "field does not apply to this bike",
    a NULL value means "applies, not yet sourced"

— but the loop underneath never omitted anything, so that distinction was never
actually made. Every bike got every column. A shaft-drive VT1100C Shadow ended
up carrying Chain Pitch, Chain Length, Front Sprocket and Rear Sprocket, and at
the same time carrying neither of the two driveshaft oil fields that its drive
type does call for.

The questionnaire has always had this right: q3 option A hands out the chain
fields and option C the driveshaft ones. Nothing was wrong with the branching.
The import simply bypassed it, and the affected bikes have never been through
the questionnaire, so nothing has had occasion to correct them since.

What this does, per bike, using only the bike's own recorded Drive Type:

  * removes drive rows that contradict it — chain hardware on a shaft bike
  * adds the rows it implies and is missing, NULL, as "applies, not sourced"

Bikes whose Drive Type is unknown are left completely alone; guessing is what
caused this. A contradicting row that somebody has actually filled in is also
left alone and reported instead — a real value beats an inferred drive type, and
destroying sourced data on an inference is exactly the wrong trade.

Additive where it can be, idempotent either way. Safe to re-run.

Run:  py migrate_drive_type_specs.py [path/to/data.db]
"""
import os
import sqlite3
import sys

DEFAULT_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")

# Keyed by the value the catalog recorded in the drive_type spec. The field
# lists mirror data/questionnaire.json q3 — if that question changes, this
# should change with it.
DRIVE_FIELDS = {
    "Chain and sprockets": [
        "drive_chain", "chain_pitch", "chain_length_links",
        "master_link_type", "front_sprocket", "rear_sprocket",
    ],
    "Driveshaft": [
        "drive_shaft_oil_weight", "drive_shaft_oil_volume",
    ],
    "Belt drive": [
        "drive_belt",
    ],
}


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    # Only fields this script knows how to place. Anything outside the map is
    # none of its business, brakes and tyres included.
    known = sorted({f for fs in DRIVE_FIELDS.values() for f in fs})

    # Fields must exist before rows can point at them.
    have = {r[0] for r in conn.execute(
        "SELECT field_key FROM spec_fields WHERE field_key IN (%s)"
        % ",".join("?" * len(known)), known)}
    missing_defs = [f for f in known if f not in have]
    if missing_defs:
        print("no such spec_fields, skipping those:", ", ".join(missing_defs))

    bikes = conn.execute(
        "SELECT bike_id, value FROM specs"
        " WHERE field_key='drive_type' AND value IS NOT NULL AND TRIM(value)<>''"
    ).fetchall()

    removed = added = 0
    kept_because_filled = []
    unknown_drive = 0

    for row in bikes:
        wanted = DRIVE_FIELDS.get((row["value"] or "").strip())
        if wanted is None:
            unknown_drive += 1
            continue
        wanted = [f for f in wanted if f in have]
        contradicts = [f for f in known if f not in wanted]

        # Remove what the drive type rules out — but never a row someone has
        # filled in. A sourced value is evidence; the drive type is an import
        # artifact, and where they disagree the evidence wins.
        for f in contradicts:
            hit = conn.execute(
                "SELECT id, value FROM specs WHERE bike_id=? AND field_key=?",
                (row["bike_id"], f)).fetchone()
            if hit is None:
                continue
            if hit["value"] is not None and hit["value"].strip():
                kept_because_filled.append(
                    (row["bike_id"], f, hit["value"], row["value"]))
                continue
            conn.execute("DELETE FROM specs WHERE id=?", (hit["id"],))
            removed += 1

        # Add what it calls for and the bike does not have. NULL value with
        # 'pending' confidence is the import's own vocabulary for "applies to
        # this bike, nobody has sourced it yet".
        for f in wanted:
            exists = conn.execute(
                "SELECT 1 FROM specs WHERE bike_id=? AND field_key=?",
                (row["bike_id"], f)).fetchone()
            if exists:
                continue
            conn.execute(
                "INSERT INTO specs (bike_id, field_key, value, confidence)"
                " VALUES (?,?,NULL,'pending')", (row["bike_id"], f))
            added += 1

    conn.commit()

    print(f"bikes with a recorded drive type : {len(bikes)}")
    if unknown_drive:
        print(f"  drive type not in the map      : {unknown_drive} (left alone)")
    print(f"contradicting rows removed       : {removed}")
    print(f"implied rows added               : {added}")
    if kept_because_filled:
        print(f"contradicting but FILLED, kept   : {len(kept_because_filled)}")
        for bike_id, f, val, dt in kept_because_filled:
            print(f"    bike {bike_id}: {f} = {val!r} on a {dt!r} bike")
        print("  Someone sourced these. Check whether the value or the drive")
        print("  type is the one that is wrong; this script will not guess.")

    left = conn.execute("""
        SELECT COUNT(*) FROM specs s
        JOIN specs d ON d.bike_id = s.bike_id AND d.field_key = 'drive_type'
        WHERE d.value = 'Driveshaft'
          AND s.field_key IN ('chain_pitch','chain_length_links',
                              'front_sprocket','rear_sprocket','drive_chain',
                              'master_link_type')
    """).fetchone()[0]
    print(f"chain rows still on shaft bikes  : {left}")
    print("fk_check :", conn.execute("PRAGMA foreign_key_check").fetchall() or "clean")
    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
