"""
Read-only audit of the Spec Tree: where the fields a bike carries disagree
with what the bike said about itself.

A bike collects its fields from two places -- the questionnaire's own lists,
and the field_triggers table -- and when those two disagree a bike ends up
listing a spec it has no business listing. That is how every bike with a
front disc came to carry two brake-pad fields at once.

Five things get checked:

  A. NEAR-DUPLICATE LABELS   two fields whose names are the same words in a
                             different order ("Front Left Brake Pads" /
                             "Front Brake Pad Left"). Not proof of a fault,
                             but this is what one looks like from the outside.
  B. TRIGGERS THAT CONTRADICT THE QUESTIONNAIRE
                             a trigger puts a field on one answer, the
                             questionnaire puts it on another. One of them is
                             wrong, and the bikes show it.
  C. TRIGGERS THE QUESTIONNAIRE NEVER MENTIONS
                             fine if the field was added later by an approved
                             proposal; a mistake if it was meant to be part of
                             the walk. Listed so a person can tell which.
  D. PROMISED FIELDS THAT DO NOT EXIST
                             the questionnaire names a field that is not on
                             the tree, so that answer silently delivers
                             nothing.
  E. ROWS NOTHING JUSTIFIES  a bike carries a field that neither its answers
                             nor any trigger nor a universal field explains.

Changes nothing. Ever.

Run:  py audit_spec_tree.py [path/to/data.db]
"""
import collections
import io
import json
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")
QUESTIONNAIRE = os.path.join(ROOT, "data", "questionnaire.json")


def key_of(label):
    """The field key the app derives from a questionnaire label. Must stay in
    step with the same expression in app.py."""
    return re.sub(r"[^a-z0-9]+", "_",
                  label.lower().replace("×", " x ").replace("&", " and ")).strip("_")


def words_of(label):
    """A label reduced to the bag of words it is made of, so that two names
    built from the same words in a different order collide."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", label.lower()):
        out.add(w[:-1] if len(w) > 3 and w.endswith("s") else w)
    return frozenset(out)


def load_questionnaire():
    """(question, option) -> the field keys that answer is supposed to bring,
    plus the labels that do not resolve to any field at all."""
    d = json.load(io.open(QUESTIONNAIRE, encoding="utf-8"))
    qs = d.get("questions", d)
    brings, labels = {}, {}
    for qid, q in qs.items():
        if not isinstance(q, dict) or "options" not in q:
            continue
        for o in q["options"]:
            keys = set()
            for _cat, names in (o.get("fields") or {}).items():
                for name in names:
                    keys.add(key_of(name))
                    labels[key_of(name)] = name
            if keys:
                brings[(qid, o["l"])] = keys
    return brings, labels


def audit(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    fields = {r["field_key"]: r for r in conn.execute(
        "SELECT field_key, label, category, universal FROM spec_fields")}
    brings, promised = load_questionnaire()
    triggers = collections.defaultdict(set)
    trigger_of = collections.defaultdict(set)
    for r in conn.execute("SELECT field_key, question_id, option_label FROM field_triggers"):
        triggers[(r["question_id"], r["option_label"])].add(r["field_key"])
        trigger_of[r["field_key"]].add((r["question_id"], r["option_label"]))
    quest_of = collections.defaultdict(set)
    for where, keys in brings.items():
        for k in keys:
            quest_of[k].add(where)

    rows_on = {k: conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (k,)).fetchone()[0]
               for k in fields}

    print("=" * 74)
    print("A. NEAR-DUPLICATE LABELS -- the same words, a different order")
    print("=" * 74)
    groups = collections.defaultdict(list)
    for k, f in fields.items():
        groups[words_of(f["label"])].append(k)
    dupes = {w: ks for w, ks in groups.items() if len(ks) > 1}
    if not dupes:
        print("  none")
    for _w, ks in sorted(dupes.items(), key=lambda x: -max(rows_on[k] for k in x[1])):
        print()
        for k in sorted(ks, key=lambda k: -rows_on[k]):
            print(f"  {fields[k]['label']:34} {k:26} on {rows_on[k]:5} bikes")
        both = None
        for a in ks:
            for b in ks:
                if a >= b:
                    continue
                n = conn.execute(
                    "SELECT COUNT(DISTINCT x.bike_id) FROM specs x JOIN specs y"
                    " ON y.bike_id = x.bike_id WHERE x.field_key=? AND y.field_key=?",
                    (a, b)).fetchone()[0]
                if n:
                    both = f"  -> {n} bike(s) carry both {a} and {b}"
        print(both or "  -> no bike carries more than one of these")

    print()
    print("=" * 74)
    print("B. TRIGGERS THAT CONTRADICT THE QUESTIONNAIRE")
    print("=" * 74)
    found = 0
    for key, wheres in sorted(trigger_of.items()):
        if key not in quest_of:
            continue
        for (qid, opt) in sorted(wheres):
            if (qid, opt) in quest_of[key]:
                continue
            says = ", ".join(f"{q}={o}" for q, o in sorted(quest_of[key]))
            hit = conn.execute(
                "SELECT COUNT(*) FROM specs s JOIN bike_answers a ON a.bike_id = s.bike_id"
                " WHERE s.field_key=? AND a.question_id=? AND a.option_label=?",
                (key, qid, opt)).fetchone()[0]
            filled = conn.execute(
                "SELECT COUNT(s.value) FROM specs s JOIN bike_answers a ON a.bike_id = s.bike_id"
                " WHERE s.field_key=? AND a.question_id=? AND a.option_label=?",
                (key, qid, opt)).fetchone()[0]
            print(f"  {fields.get(key, {'label': key})['label']:30} ({key})")
            print(f"      trigger says {qid}={opt:2}   questionnaire says {says}")
            print(f"      {hit} bike(s) answered {qid}={opt} and carry it; {filled} have a value")
            found += 1
    if not found:
        print("  none")

    print()
    print("=" * 74)
    print("C. TRIGGERS THE QUESTIONNAIRE NEVER MENTIONS  (later additions, or strays)")
    print("=" * 74)
    loose = collections.defaultdict(list)
    for key, wheres in trigger_of.items():
        if key in quest_of:
            continue
        for w in wheres:
            loose[key].append(w)
    for key in sorted(loose):
        where = ", ".join(f"{q}={o}" for q, o in sorted(loose[key]))
        label = fields.get(key, {"label": "(no such field!)"})["label"]
        print(f"  {label:30} ({key}) on {rows_on.get(key, 0):5} bikes   <- {where}")
    if not loose:
        print("  none")

    print()
    print("=" * 74)
    print("D. FIELDS THE QUESTIONNAIRE PROMISES THAT DO NOT EXIST")
    print("=" * 74)
    missing = sorted(k for k in promised if k not in fields)
    for k in missing:
        covered = [f"{q}={o}" for (q, o), keys in brings.items() if k in keys]
        print(f'  "{promised[k]}"')
        print(f"      would be {k}   offered on {', '.join(covered)}")
    if not missing:
        print("  none")

    print()
    print("=" * 74)
    print("E. ROWS NOTHING JUSTIFIES")
    print("=" * 74)
    universal = {k for k, f in fields.items() if f["universal"]}
    answers = collections.defaultdict(dict)
    for r in conn.execute("SELECT bike_id, question_id, option_label FROM bike_answers"):
        answers[r["bike_id"]][r["question_id"]] = r["option_label"]
    unjustified = collections.Counter()
    for r in conn.execute("SELECT bike_id, field_key FROM specs"):
        bike, key = r["bike_id"], r["field_key"]
        if key in universal:
            continue
        ok = False
        for qid, opt in answers.get(bike, {}).items():
            if key in brings.get((qid, opt), ()) or key in triggers.get((qid, opt), ()):
                ok = True
                break
        if not ok:
            unjustified[key] += 1
    if not unjustified:
        print("  none")
    for key, n in unjustified.most_common(25):
        label = fields.get(key, {"label": key})["label"]
        print(f"  {label:34} {key:26} {n:6} bike(s)")
    if len(unjustified) > 25:
        print(f"  ... and {len(unjustified) - 25} more fields")
    print()
    print(f"  {sum(unjustified.values())} rows in total across {len(unjustified)} fields")
    conn.close()


if __name__ == "__main__":
    audit(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
