"""
One front brake pad spec per bike, not two.

The Spec Tree carries five front-pad fields, and each is right for a
different front brake. What the questionnaire says:

    q20=A   one caliper, left        -> Front Left Brake Pads
    q20=B   one caliper, right       -> Front Right Brake Pads
    q20=C   two calipers, and then
              q20a=A  same pads      -> Front Brake Pads      (one part, x2)
              q20a=B  different pads -> Front Brake Pad Left + Front Brake Pad Right
    q20=D   drum                     -> no pads at all; shoes instead

Three rows in field_triggers pointed at the wrong answer, and because a
bike collects fields from the questionnaire AND from field_triggers, the
wrong one landed on top of the right one:

    front_brake_pad_left    fired on q20=A  (belongs on q20a=B)
    front_brake_pad_right   fired on q20=B  (belongs on q20a=B)
    front_left_brake_pads   fired on q20=C  (belongs on q20=A)

So every single-disc bike listed both "Front Left Brake Pads" and "Front
Brake Pad Left", and every twin-disc bike listed both "Front Brake Pads"
and "Front Left Brake Pads" -- two rows for one set of pads, which is
exactly the wrong spec this site exists to stop.

This fixes the three triggers so it stops happening, then clears the rows
that should never have been created. It only ever deletes an EMPTY row:
no value, no alternative, no flag, no vote, no request, no note. Anything
a rider actually filled in is kept and reported instead -- a spec
somebody sourced is not this script's to throw away. A bike whose q20
answer is missing or "not sure" is left alone entirely; nothing here
guesses what brake a bike has.

Idempotent: the second run has nothing to do.

Run:  py migrate_front_brake_pads.py [path/to/data.db]
"""
import collections
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")

PAD_FIELDS = ("front_left_brake_pads", "front_right_brake_pads", "front_brake_pads",
              "front_brake_pad_left", "front_brake_pad_right")

# (field_key, wrong question, wrong option, right question, right option)
TRIGGER_FIXES = (
    ("front_brake_pad_left",  "q20", "A", "q20a", "B"),
    ("front_brake_pad_right", "q20", "B", "q20a", "B"),
    ("front_left_brake_pads", "q20", "C", "q20",  "A"),
)


def pads_for(answers):
    """The front pad fields this bike's answers call for, or None when the
    answers do not say -- unanswered, or "not 100% sure"."""
    q20, q20a = answers.get("q20"), answers.get("q20a")
    if q20 == "A":
        return {"front_left_brake_pads"}
    if q20 == "B":
        return {"front_right_brake_pads"}
    if q20 == "C":
        if q20a == "A":
            return {"front_brake_pads"}
        if q20a == "B":
            return {"front_brake_pad_left", "front_brake_pad_right"}
        return None                      # twin discs, but nobody said which pads
    if q20 == "D":
        return set()                     # drum: shoes, no pads
    return None


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Everything below reads first and writes afterwards, so the backup in the
    # middle is taken on a connection with no transaction open. conn.backup()
    # blocks forever against its own uncommitted write.

    # ---- 1. the triggers that point at the wrong answer ------------------
    wrong_triggers = [t for t in TRIGGER_FIXES if conn.execute(
        "SELECT 1 FROM field_triggers WHERE field_key=? AND question_id=? AND option_label=?",
        (t[0], t[1], t[2])).fetchone()]

    # ---- 2. what each bike answered, and what it actually carries -------
    answers = collections.defaultdict(dict)
    for r in conn.execute("SELECT bike_id, question_id, option_label FROM bike_answers"
                          " WHERE question_id IN ('q20','q20a')"):
        answers[r["bike_id"]][r["question_id"]] = r["option_label"]

    ph = ",".join("?" * len(PAD_FIELDS))
    carried = collections.defaultdict(list)
    for r in conn.execute(f"SELECT id, bike_id, field_key, value FROM specs"
                          f" WHERE field_key IN ({ph})", PAD_FIELDS):
        carried[r["bike_id"]].append(r)

    # ---- 3. which rows hold something a rider put there ------------------
    busy = set()
    for table in ("spec_alternates", "value_flags", "spec_votes", "spec_requests"):
        for r in conn.execute(f"SELECT DISTINCT spec_id FROM {table}"):
            busy.add(r[0])
    noted = {(r["bike_id"], r["field_key"]) for r in conn.execute(
        f"SELECT bike_id, field_key FROM spec_notes WHERE field_key IN ({ph})", PAD_FIELDS)}

    doomed, kept, unknown = [], [], 0
    for bike_id, specs in carried.items():
        want = pads_for(answers.get(bike_id, {}))
        if want is None:
            unknown += 1
            continue
        for s in specs:
            if s["field_key"] in want:
                continue
            filled = (s["value"] is not None and s["value"] != "")
            if filled or s["id"] in busy or (bike_id, s["field_key"]) in noted:
                kept.append((bike_id, s["field_key"], s["value"]))
            else:
                doomed.append((bike_id, s["id"], s["field_key"]))

    # Nothing here is recoverable from the app, so take a copy of the database
    # before removing a single row -- and only when there is something to
    # remove, so a re-run does not litter the disk with copies. sqlite's own
    # backup, never a file copy: the file on disk may be mid-write.
    if doomed:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        spare = f"{db_path}.bak-{stamp}-before-front-brake-pads"
        out = sqlite3.connect(spare)
        try:
            conn.backup(out)
        finally:
            out.close()
        print(f"database copied to {spare} before touching anything")

    # ---- 4. now the writes, triggers first so nothing re-creates a row --
    moved = 0
    for key, bad_q, bad_o, good_q, good_o in wrong_triggers:
        conn.execute(
            "DELETE FROM field_triggers WHERE field_key=? AND question_id=? AND option_label=?",
            (key, bad_q, bad_o))
        conn.execute(
            "INSERT OR IGNORE INTO field_triggers (field_key, question_id, option_label)"
            " VALUES (?,?,?)", (key, good_q, good_o))
        moved += 1

    for bike_id, spec_id, key in doomed:
        conn.execute("DELETE FROM specs WHERE id=?", (spec_id,))
    # A field that has left a bike takes the manager's per-bike choices about
    # it with it, the same as removing one by hand does.
    for bike_id, _spec_id, key in doomed:
        if not conn.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key=?",
                            (bike_id, key)).fetchone():
            conn.execute("DELETE FROM bike_spec_categories WHERE bike_id=? AND field_key=?",
                         (bike_id, key))
            conn.execute("DELETE FROM bike_header_specs WHERE bike_id=? AND field_key=?",
                         (bike_id, key))
    conn.commit()

    # ---- 5. say what happened -------------------------------------------
    print(f"triggers moved to the right answer: {moved}")
    print(f"duplicate pad rows removed:         {len(doomed)}")
    by_field = collections.Counter(k for _b, _i, k in doomed)
    for key, n in by_field.most_common():
        print(f"    {key:24} {n}")
    if unknown:
        print(f"bikes left alone (q20 unanswered or unsure): {unknown}")
    if kept:
        print(f"KEPT, because somebody had filled them in -- look at these by hand: {len(kept)}")
        for bike_id, key, value in kept[:20]:
            print(f"    bike {bike_id}  {key}  = {value!r}")

    left = conn.execute(
        f"SELECT bike_id, COUNT(*) n FROM specs WHERE field_key IN ({ph})"
        f" GROUP BY bike_id HAVING n > 1", PAD_FIELDS).fetchall()
    twins = [r for r in left if r["n"] > 1]
    # Two rows is right for one bike in one case only: twin discs with
    # different pads left and right.
    real_dupes = []
    for r in twins:
        want = pads_for(answers.get(r["bike_id"], {}))
        if want is None or len(want) < r["n"]:
            real_dupes.append(r["bike_id"])
    print(f"bikes still carrying more than one front pad field: {len(real_dupes)}")
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
