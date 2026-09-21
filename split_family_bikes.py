"""Split every imported family bike into one bike per engine size.

    py split_family_bikes.py [--dry-run] [--bikez <dir of <make>.json>]

The model lists grouped a family under one row where the machines share a
frame and engine design -- "XJ550 / XJ650 / XJ700 / XJ750 / XJ900" -- and
the import made that one bike, with the largest displacement in the family
recorded as its engine size. One bike per engine size is the rule now: a
650 and a 900 are not the same machine, and a rider looking for XJ650 specs
should not land on a page that says 891cc.

For each family bike (its displacement value says "largest in the family"):
  * the names are grouped by the size in the name -- XJ650 and XJ650 Seca
    together, XJ900 apart; a name with no size ("Custom", "Silverado") stays
    with the size named just before it
  * sizes within 2% of each other are one group (BN 302 / TNT 300; the
    Fantic 241 / 243 / 245 trials models)
  * each group becomes a bike: make, its own model code (the first name in
    the group), every field the family had (copied empty), the family's
    questionnaire answers, and the same bike type
  * years come from the bikez.com index where a group's names are found
    there, clipped to the family's span; otherwise the family's span
  * engine size: the group that matches the family's recorded size keeps that
    exact figure; every other group gets its nominal size, marked
    "(nominal)" because the exact figure has not been checked
  * the family bike is deleted (its names, years, specs and answers cascade)

Nothing hand-entered is touched: only bikes created by the import, none of
which has a manager, a value beyond the two the import wrote, or a vote.
"""
import datetime
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import defaultdict

from import_model_lists import THIS_YEAR

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(ROOT, "data.db")
NUM = re.compile(r"(?<![\d.])(\d{2,4})(?![\d.])")

# Names whose number is not their engine size. Triumph's T-numbers (T150,
# T509, TR65) are handled by rule below; these are the one-offs.
NOT_A_SIZE = {
    ("Triumph", "Tiger 850 Sport"): 888,   # the 888 triple, detuned
    ("Yamaha", "DT-1"): 246,
    ("Yamaha", "R5"): 347,
    ("Vespa", "GT60"): 244,                # 60th anniversary GT, a 250
    ("Suzuki", "T20 X6 Hustler"): 247,
    ("Suzuki", "T10"): 250,
}


# ---------------------------------------------------------------------------
# Reading a family name
# ---------------------------------------------------------------------------
def variants(text):
    """'A / B / C' -> [A, B, C]. Commas inside a variant are kept, and a
    slash inside parentheses does not split: "V-twins (120 / 140)" is one
    name."""
    out, buf, depth = [], "", 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        buf += ch
        if depth == 0 and buf.endswith(" / "):
            out.append(buf[:-3].strip()); buf = ""
    out.append(buf.strip())
    return [v for v in out if v]


def disp_of(v, make=None):
    """The displacement-like number in a name, or None. 1900-2100 is a year or
    a model name (Derbi 2002), not a size. Suzuki's Boulevard S50 / M109R /
    C90 are cubic inches."""
    if (make, v.strip()) in NOT_A_SIZE:
        return NOT_A_SIZE[(make, v.strip())]
    if make == "Triumph" and re.search(r"(?<![A-Za-z])TR?" + r"\d{2,3}[A-Za-z]?(?![A-Za-z0-9])", v):
        return None                        # T150, T160, TR65, T509, T595, T100: model numbers
    if make == "Suzuki":
        m = re.search(r"(?:Boulevard\s+)?[SMC](\d{2,3})R?", v)
        if m and ("Boulevard" in v or re.fullmatch(r"[SMC]\d{2,3}R?", v.strip())):
            return round(int(m.group(1)) * 16.387)
    for n in NUM.findall(v):
        if 50 <= int(n) < 1900:
            return int(n)
    return None


def stem_style(names):
    """Whether the family writes '250 XC' (number first) or 'XJ650' (number last)."""
    for v in names:
        if disp_of(v) is not None and re.search(r"[A-Za-z]", v):
            return "prefix" if re.match(r"^\d", v) else "suffix"
    return "suffix"


def expand(names):
    """Give bare-number variants their model name: 'TT-R50 / 90 / 110' ->
    TT-R50, TT-R90, TT-R110; '250 / 300 XC' -> 250 XC, 300 XC."""
    out = []
    if stem_style(names) == "prefix":
        pending = []
        for v in names:
            if re.fullmatch(r"\d{2,4}", v):
                pending.append(v)
                continue
            m = re.match(r"^(\d{2,4})\s*(.*)$", v)
            if m and pending and m.group(1) not in pending:
                out += [f"{p} {m.group(2)}".strip() for p in pending]
                pending = []
            elif pending:
                out += pending          # "354 / 354 Sport": the bare 354 is a model
                pending = []
            out.append(v)
        out += pending
    else:
        stems = {}                      # size -> stem, so "302 S" follows "BN 302"
        stem = None
        for v in names:
            m = re.match(r"^(.*?)[\s-]*(\d{2,4})([A-Za-z]{0,3})$", v)
            if m and m.group(1).strip():
                stem = m.group(1).strip()
                stems[int(m.group(2))] = stem
                out.append(v)
            elif re.match(r"^\d{2,4}[A-Za-z]{0,3}(\s.*|\s*\(.*)?$", v) and stem:
                n = int(re.match(r"^(\d{2,4})", v).group(1))
                use = stems.get(n, stem)
                joined = any(re.match(rf"^{re.escape(use)}\d", x) for x in names)
                out.append(f"{use}{'' if joined else ' '}{v}")
            else:
                out.append(v)
    return out


def groups_for(model_text, make=None):
    """[(size, [names])], in the order the sizes first appear. "A and B"
    joins two model lines -- the two-stroke XCs and the four-stroke XC-Fs --
    that are grouped separately and never merged, even at the same size."""
    out = []
    for line in re.split(r"\s+and\s+", model_text):
        names = expand(variants(line))
        groups, order, last, unassigned = {}, [], None, []
        for v in names:
            d = disp_of(v, make)
            if d is None:
                if last is None:
                    unassigned.append(v); continue
                d = last
            last = d
            # within 3.5% of a size already seen -> the same engine (VS1400 /
            # S83, GTS 300 / 310); 250 / 260 stays apart
            for seen in order:
                if abs(seen - d) <= max(3, 0.035 * seen):
                    d = seen; break
            if d not in groups:
                groups[d] = []; order.append(d)
            groups[d].append(v)
        if unassigned and order:
            groups[order[0]] = unassigned + groups[order[0]]
        out += [(d, groups[d]) for d in order]
    return out


# ---------------------------------------------------------------------------
# Years from the bikez index
# ---------------------------------------------------------------------------
def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_bikez(directory, make):
    slug = {"Harley-Davidson": "harley", "Moto Guzzi": "motoguzzi", "MV Agusta": "mvagusta",
            "GasGas": "gasgas"}.get(make, make.lower().replace(" ", ""))
    path = os.path.join(directory, f"{slug}.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    prefix = norm(make)
    out = {}
    for name, years in data.items():
        n = norm(name)
        if n.startswith(prefix):
            n = n[len(prefix):]
        out[n] = years
    return out


def years_from_bikez(index, names, y0, y1):
    """(first, last) for a group, from the index entries its names match,
    clipped to the family's span. None if nothing matches."""
    last_year = THIS_YEAR if y1 is None else y1
    found = set()
    for name in names:
        # "250 XC, XC-W" is two names; "(trials)" and "(India)" are not part of one
        for piece in re.split(r",\s*", re.sub(r"\s*\(.*?\)", "", name)):
            v = norm(piece)
            if len(v) < 3:
                continue
            strict = [ys for n, ys in index.items() if re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![a-z0-9])", n)]
            loose = strict or [ys for n, ys in index.items() if re.search(rf"(?<![a-z0-9]){re.escape(v)}(?![0-9])", n)]
            for ys in loose:
                found.update(y for y in ys if y0 <= y <= last_year)
    # bikez lists many small makes thinly -- one catalogue year for a model
    # built for ten. Fewer than three matched years is not evidence of a span.
    if len(found) < 3:
        return None
    first, last = min(found), max(found)
    if y1 is None and last >= THIS_YEAR - 2:
        last = None
    return first, last


# ---------------------------------------------------------------------------
def main(argv):
    dry = "--dry-run" in argv
    bikez_dir = argv[argv.index("--bikez") + 1] if "--bikez" in argv else None

    if not dry:
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with sqlite3.connect(DB_PATH) as src, sqlite3.connect(f"{DB_PATH}.bak-{stamp}") as dst:
            src.backup(dst)      # the backup API sees the WAL; a file copy would not
        print(f"backup: data.db.bak-{stamp}")

    conn = sqlite3.connect(DB_PATH, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN")

    families = [dict(r) for r in conn.execute(
        "SELECT b.*, n.name AS primary_name, s.value AS disp, s.id AS disp_id"
        " FROM bikes b JOIN bike_names n ON n.bike_id=b.id AND n.is_primary=1"
        " JOIN specs s ON s.bike_id=b.id AND s.field_key='engine_displacement'"
        " WHERE s.value LIKE '%largest in the family%' ORDER BY b.make, b.id")]

    index_cache = {}
    created = removed = 0
    by_years = {"bikez": 0, "family": 0}
    renamed = []
    for fam in families:
        make = fam["make"]
        model_text = fam["primary_name"][len(make) + 1:]
        gs = groups_for(model_text, make)
        fam_cc = int(re.match(r"(\d+)", fam["disp"]).group(1))
        if len(gs) < 2:
            # one engine after all (Benelli 250 Quattro / 254): just drop the marker
            conn.execute("UPDATE specs SET value=? WHERE id=?", (f"{fam_cc}cc", fam["disp_id"]))
            print(f"  = #{fam['id']} {fam['primary_name']}: one size, kept; displacement now {fam_cc}cc")
            continue
        if bikez_dir and make not in index_cache:
            index_cache[make] = load_bikez(bikez_dir, make)
        index = index_cache.get(make, {})

        # the family's exact figure belongs to the group nearest its size
        nearest = min(gs, key=lambda g: abs(g[0] - fam_cc))[0]
        if abs(nearest - fam_cc) > 0.1 * fam_cc:
            nearest = None

        existing = [dict(r) for r in conn.execute(
            "SELECT id, model_code, year_start, year_end FROM bikes WHERE make=? AND id<>?",
            (make, fam["id"]))]
        old_specs = [dict(r) for r in conn.execute(
            "SELECT field_key, value, confidence FROM specs WHERE bike_id=?", (fam["id"],))]
        old_answers = [dict(r) for r in conn.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?", (fam["id"],))]

        # The family goes first so its own code and start year are free for
        # the group that inherits them; everything it had was read above.
        conn.execute("DELETE FROM bikes WHERE id=?", (fam["id"],))
        removed += 1

        for size, names in gs:
            span = years_from_bikez(index, names, fam["year_start"], fam["year_end"]) if index else None
            if span:
                y0, y1 = span; by_years["bikez"] += 1
            else:
                y0, y1 = fam["year_start"], fam["year_end"]; by_years["family"] += 1
            code = names[0]
            clash = [e for e in existing if e["model_code"].lower() == code.lower()
                     and y0 <= (THIS_YEAR if e["year_end"] is None else e["year_end"])
                     and e["year_start"] <= (THIS_YEAR if y1 is None else y1)]
            if clash:
                code = f"{code} ({fam['model_code']})"
                renamed.append(f"{make} {code}")
            primary = f"{make} {' / '.join(names)}"
            disp = f"{fam_cc}cc" if size == nearest else f"{size}cc (nominal)"

            cur = conn.execute(
                "INSERT INTO bikes (make, model_code, year_start, year_end, bike_type, years_verified)"
                " VALUES (?,?,?,?,?,0)", (make, code, y0, y1, fam["bike_type"]))
            bid = cur.lastrowid
            conn.execute("INSERT INTO bike_names (bike_id, name, market, is_primary) VALUES (?,?,'',1)",
                         (bid, primary))
            for n in names:
                if f"{make} {n}" != primary:
                    conn.execute("INSERT OR IGNORE INTO bike_names (bike_id, name, market, is_primary)"
                                 " VALUES (?,?,'',0)", (bid, f"{make} {n}"))
            for y in range(y0, (THIS_YEAR if y1 is None else y1) + 1):
                conn.execute("INSERT INTO bike_years (bike_id, year, market) VALUES (?,?,'')", (bid, y))
            for s in old_specs:
                value = disp if s["field_key"] == "engine_displacement" else s["value"]
                conn.execute("INSERT INTO specs (bike_id, field_key, value, confidence) VALUES (?,?,?,?)",
                             (bid, s["field_key"], value, s["confidence"]))
            conn.executemany(
                "INSERT INTO bike_answers (bike_id, question_id, option_label, answered_by) VALUES (?,?,?,NULL)",
                [(bid, a["question_id"], a["option_label"]) for a in old_answers])
            existing.append({"id": bid, "model_code": code, "year_start": y0, "year_end": y1})
            created += 1
            if dry:
                print(f"  + {primary}  {y0}-{y1 or 'present'}  {disp}")

    print(f"\n{'DRY RUN -- nothing written' if dry else 'written'}: {removed} family bikes ->"
          f" {created} bikes ({by_years['bikez']} with years from bikez, {by_years['family']} keeping the family span)")
    if renamed:
        print(f"{len(renamed)} model codes carried the family code to avoid a clash:")
        for r in renamed:
            print("  " + r)
    print("bikes in database:", conn.execute("SELECT COUNT(*) FROM bikes").fetchone()[0])
    conn.execute("ROLLBACK" if dry else "COMMIT")
    conn.close()


if __name__ == "__main__":
    main(sys.argv[1:])
