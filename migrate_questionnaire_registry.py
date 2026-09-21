"""
Make the questionnaire and the field registry agree again.

build_spec_tree refuses to invent a field it has not seen:

    raise HttpError(500, f"questionnaire triggered unregistered field {label!r}")

Two paths through the questionnaire were hitting that, so answering them
returned a 500 instead of a spec sheet.

1. CHAIN PITCH. "Chain and sprockets" (q3=A) still asked for Chain Pitch after
   the field was deleted from the tree on 2026-08-30. Deleting a field does not
   touch data/questionnaire.json, so the most common drive type on the platform
   could not be answered at all. The deletion was deliberate, so the fix is to
   stop asking for it rather than to bring the field back.

2. FIVE AND SIX CARBURETTORS. Options B to E enumerate their carbs -- "Carb 1
   Pilot Jet", "Carb 2 Pilot Jet" and so on -- but F and G asked for a range,
   "Carb 1-5 Pilot Jet", which was never a field and never could be: a range
   gives one box for five carburettors, so four of the five have nowhere to put
   a value. They are enumerated here like the others, which needs Carb 5 and
   Carb 6 to exist.

Neither path had ever worked. No bike has answered either way -- if one had,
the 500 would have stopped it -- so nothing is being repaired retroactively.

Additive and idempotent: the JSON edit is skipped once applied, and the fields
are inserted only when absent.

Run:  py migrate_questionnaire_registry.py [path/to/data.db]
"""
import io
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")
QFILE = os.path.join(ROOT, "data", "questionnaire.json")

NEW_FIELDS = [
    ("carb_5_pilot_jet", "Carb 5 Pilot Jet", "Fuel and Air"),
    ("carb_5_main_jet",  "Carb 5 Main Jet",  "Fuel and Air"),
    ("carb_6_pilot_jet", "Carb 6 Pilot Jet", "Fuel and Air"),
    ("carb_6_main_jet",  "Carb 6 Main Jet",  "Fuel and Air"),
]
ANCHOR = "carb_4_main_jet"


def slug(label):
    """The same key build_spec_tree derives, so this checks what it checks."""
    return re.sub(r"[^a-z0-9]+", "_",
                  label.lower().replace("×", " x ").replace("&", " and ")).strip("_")


def fix_questionnaire():
    doc = json.load(io.open(QFILE, encoding="utf-8"))
    q = doc["questions"]
    changed = []

    # 1. stop asking for a field that no longer exists
    for opt in q["q3"]["options"]:
        fields = opt.get("fields") or {}
        for cat, labels in list(fields.items()):
            if "Chain Pitch" in labels:
                labels.remove("Chain Pitch")
                changed.append("q3=%s no longer asks for Chain Pitch" % opt["l"])
                if not labels:
                    del fields[cat]
        if not fields:
            opt.pop("fields", None)

    # 2. enumerate the five- and six-carb options like every other option
    for letter, count in (("F", 5), ("G", 6)):
        opt = next(o for o in q["q7"]["options"] if o["l"] == letter)
        want = []
        for n in range(1, count + 1):
            want += ["Carb %d Pilot Jet" % n, "Carb %d Main Jet" % n]
        if (opt.get("fields") or {}).get("Fuel and Air") != want:
            opt["fields"] = {"Fuel and Air": want}
            changed.append("q7=%s now lists all %d carburettors" % (letter, count))

    if changed:
        io.open(QFILE, "w", encoding="utf-8").write(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    return changed, doc


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row

    todo = [f for f in NEW_FIELDS
            if not conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                                (f[0],)).fetchone()]
    if todo:
        anchor = conn.execute("SELECT sort_order, category FROM spec_fields"
                              " WHERE field_key=?", (ANCHOR,)).fetchone()
        cat = anchor["category"] if anchor else NEW_FIELDS[0][2]
        start = anchor["sort_order"] if anchor else conn.execute(
            "SELECT COALESCE(MAX(sort_order),0) FROM spec_fields WHERE category=?",
            (cat,)).fetchone()[0]
        conn.execute("UPDATE spec_fields SET sort_order = sort_order + ?"
                     " WHERE category=? AND sort_order > ?",
                     (len(todo) * 10, cat, start))
        for i, (key, label, _c) in enumerate(todo, start=1):
            conn.execute(
                "INSERT INTO spec_fields (field_key, label, category, spec_type,"
                " sort_order) VALUES (?,?,?,'pref',?)",
                (key, label, cat, start + i * 10))
            print(f"  registered {label!r}")
        conn.commit()
    else:
        print("  carburettor fields already registered")

    changed, doc = fix_questionnaire()
    for line in changed:
        print("  " + line)
    if not changed:
        print("  questionnaire already consistent")

    # The check that matters: walk every label any answer can trigger and
    # confirm the registry has it.
    missing = []
    for qq in doc["questions"].values():
        opts = qq.get("options") or []
        rules = qq.get("next") if isinstance(qq.get("next"), list) else []
        groups = [o.get("fields") or {} for o in opts]
        groups += [r.get("add_fields") or {} for r in rules]
        for g in groups:
            for labels in g.values():
                for raw in labels:
                    label = doc["aliases"].get(raw, raw)
                    if not conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                                        (slug(label),)).fetchone():
                        missing.append((raw, label))

    print()
    if missing:
        print("STILL UNREGISTERED — these paths will return a 500:")
        for raw, label in sorted(set(missing)):
            print(f"  {raw!r} -> {label!r} ({slug(label)})")
    else:
        print("every field the questionnaire can trigger is registered")

    print("integrity:", conn.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
