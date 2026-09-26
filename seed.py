"""
GearHeadSpecs — build data.db from schema.sql plus the files in data/.

Run:  py seed.py          (drops and rebuilds data.db from scratch)

The interesting part is import_catalog(). The source catalog is 991 flat
(Model, Year) rows, but the schema says a bike is one spec set covering
however many model years share it. So rows are folded into bikes: within a
model, a CONTIGUOUS run of years whose spec values are all identical becomes
one bike. A year where any value changes starts a new one.

Every bike created this way gets years_verified = 0. The fold is inferred from
data that carries no model-year boundaries, so its year spans are a guess until
a manager confirms them.
"""
import json
import os
import sys
import sqlite3
import hashlib
import secrets
import re
from collections import defaultdict

import questionnaire

ROOT = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(os.environ.get("DATA_DIR") or ROOT, "data.db")
DATA = os.path.join(ROOT, "data")

# Dev-only. Every seeded account gets this password so the roles can be walked
# through end to end. Change it before this is reachable by anyone else.
DEV_PASSWORD = "gearhead"

PBKDF2_ROUNDS = 200_000


# ---------------------------------------------------------------------------
# Field keys
#
# Every mockup refers to a field by its display label, so the key is derived
# from the label rather than invented. One label -> one key -> one row in
# spec_fields, which is what makes the catalog's "Front Tire Size" and the
# CB919 sheet's "Front Tire Size" the same field instead of two.
# ---------------------------------------------------------------------------
def slugify(label):
    s = label.lower()
    s = s.replace("×", " x ").replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")


# Catalog column -> the spec field it actually is. Without this map the catalog
# would create "Spark Plug (NGK)" alongside the CB919 sheet's
# "Spark Plug — Standard" and they would never line up.
CATALOG_FIELD_MAP = {
    "Drive Type":           ("Drive Type",           "Drive"),
    "Front Tire Size":      ("Front Tire Size",      "Drive"),
    "Rear Tire Size":       ("Rear Tire Size",       "Drive"),
    "Chain Pitch":          ("Chain Pitch",          "Drive"),
    "Chain Length (Links)": ("Chain Length (Links)", "Drive"),
    "Front Sprocket":       ("Front Sprocket",       "Drive"),
    "Rear Sprocket":        ("Rear Sprocket",        "Drive"),
    "Battery (Yuasa)":      ("Battery",              "Electrical"),
    "Spark Plug (NGK)":     ("Spark Plug — Standard", "Engine"),
    "Spark Plug Gap (mm)":  ("Spark Plug Gap",       "Engine"),
}

# Columns that are not specs: identity or provenance.
NON_SPEC_COLUMNS = {"Model", "Year", "Notes", "CC", "CC_note"}

CATEGORY_ORDER = ["General", "Engine", "Drive", "Brakes", "Fuel and Air",
                  "Controls", "Suspension", "Electrical", "Gear and Accessories"]


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    )
    return digest.hex(), salt


def load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Spec field registry
# ---------------------------------------------------------------------------
class FieldRegistry:
    """Collects every field the seed touches, so spec_fields is written once
    and every specs row has a parent to point at."""

    def __init__(self):
        self.fields = {}  # field_key -> dict

    def add(self, label, category, spec_type="pref", order_hint=None):
        """order_hint is the field's position in the file that defines it.

        The CB919 sheet lists its specs in a deliberate reading order — cylinder
        layout, then bore and stroke, then the plugs, then the oil. Without a
        hint the display falls back to alphabetical, which splits the two
        coolant entries apart and sorts "Spark Plug Gap" above "Spark Plug —
        Standard". The hint is what preserves the order somebody chose.
        """
        key = slugify(label)
        existing = self.fields.get(key)
        if existing:
            # First file to claim a position wins; later registrations of the
            # same field (catalog, questionnaire) do not reshuffle it.
            if order_hint is not None and existing.get("order_hint") is None:
                existing["order_hint"] = order_hint
            # A field seen twice must agree on where it lives. If it does not,
            # that is a real modelling collision and should be loud, not merged.
            if existing["category"] != category:
                raise ValueError(
                    f"field {key!r} claimed by two categories: "
                    f"{existing['category']!r} and {category!r}"
                )
            # A richer type wins: community > pref > fixed.
            rank = {"fixed": 0, "pref": 1, "community": 2}
            if rank[spec_type] > rank[existing["spec_type"]]:
                existing["spec_type"] = spec_type
            return key
        self.fields[key] = {
            "field_key": key, "label": label,
            "category": category, "spec_type": spec_type,
            "order_hint": order_hint,
        }
        return key

    def write(self, conn):
        """Assign sort_order and write.

        Numbering is sparse — 10, 20, 30 — so inserting a field between two
        others is one row update rather than renumbering the whole category.
        Categories sit in 1000-wide bands, which keeps each one's fields
        contiguous; the browse page groups by consecutive runs, so interleaving
        two categories would produce a duplicate heading.
        """
        by_category = defaultdict(list)
        for f in self.fields.values():
            by_category[f["category"]].append(f)

        rows = []
        for category, fields in by_category.items():
            cat_rank = (CATEGORY_ORDER.index(category)
                        if category in CATEGORY_ORDER else 99)
            # Hinted fields first, in file order; the rest alphabetically after.
            fields.sort(key=lambda f: (f["order_hint"] is None,
                                       f["order_hint"] if f["order_hint"] is not None else 0,
                                       f["label"]))
            for i, f in enumerate(fields, start=1):
                rows.append((f["field_key"], f["label"], f["category"],
                             f["spec_type"], cat_rank * 1000 + i * 10))

        conn.executemany(
            "INSERT INTO spec_fields (field_key, label, category, spec_type, sort_order)"
            " VALUES (?,?,?,?,?)", rows)
        return len(rows)


# ---------------------------------------------------------------------------
# Catalog -> bikes
# ---------------------------------------------------------------------------
def fingerprint(row):
    """The spec values that decide whether two model years are the same bike.

    Deliberately excludes Notes and CC_note: those describe the source record,
    not the machine, and letting them differ would split a bike for no
    mechanical reason.
    """
    return tuple(
        (col, row.get(col)) for col in sorted(CATALOG_FIELD_MAP)
    ) + (("CC", row.get("CC")),)


def fold_into_bikes(rows):
    """Group flat (Model, Year) rows into contiguous same-spec runs.

    Runs must be contiguous. A model that reverts to an earlier spec after a
    one-year change becomes three bikes, not two — otherwise a bike's
    year_start..year_end would span years it does not actually own, and the
    range shown on the page would be a lie.
    """
    by_model = defaultdict(list)
    for r in rows:
        by_model[r["Model"]].append(r)

    bikes = []
    for model, model_rows in sorted(by_model.items()):
        model_rows.sort(key=lambda r: (r["Year"] is None, r["Year"]))

        # Same model-year twice in the source: keep the first, drop the rest.
        # bike_years cannot hold a duplicate year for one bike anyway.
        seen_years, unique_rows = set(), []
        for r in model_rows:
            if r["Year"] in seen_years:
                continue
            seen_years.add(r["Year"])
            unique_rows.append(r)

        run = []
        for r in unique_rows:
            fp = fingerprint(r)
            contiguous = (
                run
                and fp == run[-1][1]
                and r["Year"] is not None
                and run[-1][0]["Year"] is not None
                and r["Year"] == run[-1][0]["Year"] + 1
            )
            if contiguous:
                run.append((r, fp))
            else:
                if run:
                    bikes.append((model, run))
                run = [(r, fp)]
        if run:
            bikes.append((model, run))
    return bikes


def import_catalog(conn, registry, v1_rows, v2_rows, cb919_model_code):
    """Import the 991-row catalog, marking which rows survived into v2."""
    # Provenance: which (Model, Year) pairs the v2 browse page still carried.
    v2_keys = {(r["Model"], r["Year"]) for r in v2_rows}

    # The CB919 is built from researched data, not from the catalog. Drop its
    # catalog rows so the two cannot both claim the same model years.
    rows = [r for r in v1_rows if r["Model"] != cb919_model_code]
    dropped = len(v1_rows) - len(rows)

    for col, (label, category) in CATALOG_FIELD_MAP.items():
        registry.add(label, category)
    registry.add("Engine Displacement", "Engine")

    groups = fold_into_bikes(rows)
    bike_count = year_count = spec_count = 0

    for model, run in groups:
        first = run[0][0]
        years = [r["Year"] for r, _ in run]
        cur = conn.execute(
            "INSERT INTO bikes (make, model_code, year_start, year_end,"
            " bike_type, years_verified) VALUES (?,?,?,?,?,0)",
            ("Honda", model, min(years), max(years), None),
        )
        bike_id = cur.lastrowid
        bike_count += 1

        # The display name a rider would recognise.
        conn.execute(
            "INSERT INTO bike_names (bike_id, name, market, is_primary)"
            " VALUES (?,?,'',1)", (bike_id, f"Honda {model}"))

        for r, _ in run:
            conn.execute(
                "INSERT INTO bike_years (bike_id, year, market, in_v2)"
                " VALUES (?,?,'',?)",
                (bike_id, r["Year"], 1 if (model, r["Year"]) in v2_keys else 0))
            year_count += 1

        # The catalog carries the chain hardware columns for every machine,
        # shaft-drive cruisers included, so writing every column would hand a
        # VT1100C Shadow a Chain Pitch and two sprockets it does not have. Only
        # the bike's own Drive Type is consulted, and only ever to skip: where
        # that column is blank nothing is skipped, because an extra empty row is
        # a smaller problem than a guess. The fields a drive type calls for but
        # the catalog cannot supply - driveshaft oil, drive belt - come from the
        # questionnaire, or from migrate_drive_type_specs.py for the bikes that
        # have not been through it.
        drive = (first.get("Drive Type") or "").strip().lower()
        skip = set()
        if drive and "chain" not in drive:
            skip = {"Chain Pitch", "Chain Length (Links)",
                    "Front Sprocket", "Rear Sprocket"}

        # One spec row per mapped column, NULL where the source had nothing.
        # Inserting the NULL rows on purpose: a missing row means "field does
        # not apply to this bike", a NULL value means "applies, not yet
        # sourced". Only the second belongs in a manager's to-do count.
        for col, (label, _cat) in CATALOG_FIELD_MAP.items():
            if col in skip:
                continue
            value = first.get(col)
            if isinstance(value, float) and value.is_integer():
                value = int(value)
            conn.execute(
                "INSERT INTO specs (bike_id, field_key, value, confidence)"
                " VALUES (?,?,?,?)",
                (bike_id, slugify(label),
                 None if value is None else str(value),
                 "mfr" if value is not None else "pending"))
            spec_count += 1

        # CC carries its own provenance flag in the source data. An inferred
        # displacement is recorded as pending, not passed off as manufacturer
        # data.
        cc = first.get("CC")
        if cc is not None:
            inferred = "inferred" in (first.get("CC_note") or "").lower()
            conn.execute(
                "INSERT INTO specs (bike_id, field_key, value, confidence)"
                " VALUES (?,?,?,?)",
                (bike_id, slugify("Engine Displacement"), f"{int(cc)}cc",
                 "pending" if inferred else "mfr"))
            spec_count += 1

    return {"bikes": bike_count, "years": year_count, "specs": spec_count,
            "cb919_rows_dropped": dropped}


# ---------------------------------------------------------------------------
# The CB919 — the one fully researched bike
# ---------------------------------------------------------------------------
def import_cb919(conn, registry, doc, users):
    bike = doc["bike"]
    cur = conn.execute(
        "INSERT INTO bikes (make, model_code, year_start, year_end, bike_type,"
        " years_verified) VALUES (?,?,?,?,?,1)",
        (bike["make"], bike["model_code"], bike["year_start"],
         bike["year_end"], bike["bike_type"]))
    bike_id = cur.lastrowid

    for n in bike["names"]:
        conn.execute(
            "INSERT INTO bike_names (bike_id, name, market, is_primary)"
            " VALUES (?,?,?,?)",
            (bike_id, n["name"], n.get("market", ""), n.get("is_primary", 0)))

    for year in range(bike["year_start"], bike["year_end"] + 1):
        conn.execute(
            "INSERT INTO bike_years (bike_id, year, market, in_v2)"
            " VALUES (?,?,'',1)", (bike_id, year))

    # Who entered what. The manager dashboard seeds flags against specs entered
    # by three different people, so those three specs need those authors for
    # the queue to make sense.
    entered_by = {
        "Front Brake Pads": users["m.alvarez"],
        "Coolant Capacity": users["cb919_dave"],
        "Starter Switch Wire Color": users["rider_kestrel99"],
    }

    spec_ids = {}
    for item in doc["specs"]:
        key = registry.add(item["label"], item["category"],
                           item.get("spec_type", "pref"))
        cur = conn.execute(
            "INSERT INTO specs (bike_id, field_key, value, confidence, tools,"
            " entered_by) VALUES (?,?,?,?,?,?)"
            # The WHERE matches the partial index that replaced the old
            # UNIQUE(bike_id, field_key): a field can now hold several rows,
            # one per year range, and at most one covering every year. The
            # seeder only ever writes the covers-every-year row.
            " ON CONFLICT(bike_id, field_key) WHERE year_from IS NULL"
            " DO UPDATE SET"
            " value=excluded.value, confidence=excluded.confidence,"
            " tools=excluded.tools, entered_by=excluded.entered_by",
            (bike_id, key, item.get("value"), item.get("confidence"),
             item.get("tools"),
             entered_by.get(item["label"], users["m.alvarez"])))
        spec_id = conn.execute(
            "SELECT id FROM specs WHERE bike_id=? AND field_key=?"
            " AND year_from IS NULL", (bike_id, key)).fetchone()[0]
        spec_ids[item["label"]] = spec_id

        for alt in item.get("alternates", []):
            conn.execute(
                "INSERT INTO spec_alternates (spec_id, text, submitted_by)"
                " VALUES (?,?,?)",
                (spec_id, alt["text"], users["cb919_dave"]))

    return bike_id, spec_ids


def seed_alternate_votes(conn, users):
    """Turn the mockup's vote counters into real vote rows.

    The counters were integers on the object. Here a vote is a row keyed by
    (alternate, user), so the same person cannot vote twice and a withdrawn
    vote is immediately correct everywhere it is displayed.
    """
    voters = [u for name, u in users.items() if name != "admin"]
    doc_votes = {}
    for item in load("cb919_specs.json")["specs"]:
        for alt in item.get("alternates", []):
            if alt.get("votes"):
                doc_votes[alt["text"]] = alt["votes"]

    placed = 0
    for offset, (text, wanted) in enumerate(doc_votes.items()):
        row = conn.execute(
            "SELECT id FROM spec_alternates WHERE text=?", (text,)).fetchone()
        if not row:
            continue
        if wanted > len(voters):
            raise ValueError(
                f"{text!r} wants {wanted} votes but only {len(voters)} users "
                f"exist to cast them — add to COMMUNITY_USERS")
        # Rotate who votes, so the same handful of accounts do not appear on
        # every single alternate.
        for i in range(wanted):
            conn.execute(
                "INSERT OR IGNORE INTO alternate_votes (alternate_id, user_id)"
                " VALUES (?,?)", (row[0], voters[(i + offset * 3) % len(voters)]))
            placed += 1
    return placed


# ---------------------------------------------------------------------------
# The named accounts the dashboards are built around.
NAMED_USERS = [
    ("admin",            "Site Admin",       "admin"),
    ("m.alvarez",        "M. Alvarez",       "manager"),
    ("cb919_dave",       "cb919_dave",       "user"),
    ("rider_kestrel99",  "rider_kestrel99",  "user"),
    ("t.moreno",         "t.moreno",         "user"),
]

# Background community accounts. A vote is a row keyed to a user, so a spec
# cannot show 22 votes unless 22 people exist to have cast them. Without these
# the seeded counts all flatten to the number of named users, every alternate
# ties, and "sorted by votes" looks broken because nothing can outrank anything.
COMMUNITY_USERS = [
    "wrenchmonkey", "sohc_sam", "two_stroke_tina", "gp_hayes", "moto_juno",
    "valveshim_vic", "dyno_dana", "kickstart_kim", "oilburner_ozzy", "torque_tess",
    "cafe_racer_cy", "greasyhands", "nightowl_nadia", "sprocket_sid", "clutch_cody",
    "highside_hana", "carb_cleaner", "airbox_ali", "pannier_pete", "fork_seal_fran",
    "rekluse_rey", "chainlube_chan", "topend_toby", "sidestand_sue",
]


def seed_users(conn):
    users = {}
    # One hash, reused. pbkdf2 at 200k rounds costs ~0.1s, and doing that 29
    # times for identical dev passwords would add ~3s to every seed run for no
    # benefit — the salt still differs per account below.
    shared_hash, shared_salt = hash_password(DEV_PASSWORD)

    for username, display, role in NAMED_USERS:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role, password_hash,"
            " password_salt) VALUES (?,?,?,?,?)",
            (username, display, role, shared_hash, shared_salt))
        users[username] = cur.lastrowid

    for username in COMMUNITY_USERS:
        cur = conn.execute(
            "INSERT INTO users (username, display_name, role, password_hash,"
            " password_salt) VALUES (?,?,'user',?,?)",
            (username, username, shared_hash, shared_salt))
        users[username] = cur.lastrowid
    return users


def seed_service_tasks(conn, registry, doc):
    for t in doc["service_tasks"]:
        interval_key = slugify(t["interval_label"]) if t.get("interval_label") else None
        conn.execute(
            "INSERT INTO service_tasks (task_key, name, interval_field_key,"
            " default_interval_miles, sort_order) VALUES (?,?,?,?,?)",
            (t["task_key"], t["name"], interval_key,
             t.get("default_interval_miles"),
             doc["service_tasks"].index(t)))
        for i, label in enumerate(t["specs"]):
            conn.execute(
                "INSERT INTO service_task_specs (task_key, field_key, sort_order)"
                " VALUES (?,?,?)", (t["task_key"], slugify(label), i))


def seed_enrichment(conn, doc, bike_id, users):
    enr = doc["enrichment"]
    field_key = slugify(enr["field_label"])
    who = {"manager": users["m.alvarez"], "user": users["cb919_dave"]}

    for t in enr["tools"]:
        conn.execute(
            "INSERT INTO spec_tools (bike_id, field_key, text, added_by)"
            " VALUES (?,?,?,?)",
            (bike_id, field_key, t["text"], who[t["added_by"]]))

    voters = [uid for name, uid in users.items() if name != "admin"]
    for offset, l in enumerate(enr["links"]):
        cur = conn.execute(
            "INSERT INTO spec_links (bike_id, field_key, link_type, title, url,"
            " added_by) VALUES (?,?,?,?,?,?)",
            (bike_id, field_key, l["link_type"], l["title"], l["url"],
             who[l["added_by"]]))
        link_id = cur.lastrowid
        wanted = l.get("votes", 0)
        if wanted > len(voters):
            raise ValueError(
                f"link {l['title']!r} wants {wanted} votes but only "
                f"{len(voters)} users exist — add to COMMUNITY_USERS")
        for i in range(wanted):
            conn.execute(
                "INSERT OR IGNORE INTO link_votes (link_id, user_id)"
                " VALUES (?,?)", (link_id, voters[(i + offset * 5) % len(voters)]))


def seed_queues(conn, users, bike_id, spec_ids):
    """The open work sitting in each dashboard when you first log in."""
    flags = [
        ("Front Brake Pads", "incorrect", "rider_kestrel99",
         "Left and right pads actually wear differently on my bike — "
         "shouldn't be one shared value."),
        ("Coolant Capacity", "irrelevant", "t.moreno",
         "This value looks like it's for the wrong sub-model."),
        ("Starter Switch Wire Color", "other", "cb919_dave",
         "Wire color changed on later production runs — worth a note?"),
    ]
    for label, reason, by, detail in flags:
        conn.execute(
            "INSERT INTO value_flags (spec_id, flagged_by, reason, detail,"
            " status) VALUES (?,?,?,?,'open')",
            (spec_ids[label], users[by], reason, detail))

    # Two shapes of "not sure", because admin resolves them differently.
    #
    # This one is not tied to a field: there is no "Ignition System" field in
    # the tree at all, which is exactly why the manager could not answer it.
    # Admin can only close it — or turn it into a branch proposal.
    conn.execute(
        "INSERT INTO not_sure_answers (bike_id, question_text, submitted_by,"
        " status, created_at) VALUES (?,?,?,'pending','2026-08-03 09:00:00')",
        (bike_id, "What ignition system does this bike have?",
         users["m.alvarez"]))

    # This one names a real field that is still a gap, so admin can confirm a
    # value and have it land on the spec.
    conn.execute(
        "INSERT INTO not_sure_answers (bike_id, field_key, question_text,"
        " submitted_by, status, created_at)"
        " VALUES (?,?,?,?,'pending','2026-08-04 11:30:00')",
        (bike_id, slugify("Engine Oil Weight"),
         "Engine Oil Weight — manual lists two grades by climate, unsure which "
         "to record as stock", users["m.alvarez"]))

    conn.execute(
        "INSERT INTO branch_proposals (field_name, category, bike_id,"
        " proposed_by, reasoning, status, created_at)"
        " VALUES (?,?,?,?,?,'pending','2026-08-05 09:00:00')",
        ("Fork Preload Adjuster Type", "Suspension", bike_id,
         users["m.alvarez"],
         "Preload adjuster style differs across sub-models and there is "
         "nowhere to record it today."))

    conn.execute(
        "INSERT INTO tree_flags (bike_id, question_text, comment, flagged_by,"
        " status) VALUES (?,?,?,?,'open')",
        (bike_id, "Front Brake Pads",
         "Pads are asked as one field but wear per side — should this be two "
         "branches rather than one shared value?",
         users["rider_kestrel99"]))


def seed_garage(conn, users, bike_id):
    """One rider with a bike and some history, so the service log has
    something to compute an overdue state from on first load."""
    year_id = conn.execute(
        "SELECT id FROM bike_years WHERE bike_id=? AND year=2005",
        (bike_id,)).fetchone()[0]
    cur = conn.execute(
        "INSERT INTO user_bikes (user_id, bike_year_id, nickname, mileage)"
        " VALUES (?,?,?,?)",
        (users["cb919_dave"], year_id, "The 919", 31450))
    ub = cur.lastrowid
    for task, date, miles in [
        ("oil",   "2026-03-14", 24800),
        ("oil",   "2025-08-02", 16900),
        ("chain", "2026-07-30", 30980),
    ]:
        conn.execute(
            "INSERT INTO service_log (user_bike_id, task_key, performed_on,"
            " miles) VALUES (?,?,?,?)", (ub, task, date, miles))


# ---------------------------------------------------------------------------
def main():
    # The Windows console defaults to cp1252, which mangles the em-dashes and
    # ± signs in the spec values this script prints. The data is UTF-8 either
    # way; this just stops the summary looking corrupted.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    with open(os.path.join(ROOT, "schema.sql"), encoding="utf-8") as f:
        conn.executescript(f.read())
    # WAL: a slow read never blocks a write. Persistent, set once per file.
    conn.execute("PRAGMA journal_mode=WAL")

    cb919_doc = load("cb919_specs.json")
    v1 = load("honda_catalog_v1.json")
    v2 = load("honda_catalog_v2.json")

    registry = FieldRegistry()

    # Fields first: every specs row needs a spec_fields parent, so the registry
    # has to be complete before anything writes to specs. Collect from both
    # sources, write once, then import.
    # The researched CB919 sheet goes in first: it is the only source with a
    # hand-chosen order, so it should be the one that decides the display.
    for i, item in enumerate(cb919_doc["specs"]):
        registry.add(item["label"], item["category"],
                     item.get("spec_type", "pref"), order_hint=i)

    for col, (label, category) in CATALOG_FIELD_MAP.items():
        registry.add(label, category)
    registry.add("Engine Displacement", "Engine")

    # Every field any path through the questionnaire can trigger. Registering
    # them here makes spec_fields a closed set: running the questionnaire later
    # creates specs rows against fields that already exist, and can never
    # invent a new branch of the tree. Growing the tree stays an admin
    # decision, made through branch_proposals.
    for label, category in questionnaire.all_triggerable_fields().items():
        registry.add(label, category)

    # The two fuel fields are on every bike and no question names them (every
    # machine in the catalog runs on gasoline, so there is nothing to ask).
    # Registered under the keys the live database uses; the labels are set
    # below, after the write, the way the migration set them.
    registry.add("Fuel Octane Grade", "General")
    registry.add("Max Ethanol", "General")

    # Gear and Accessories, last on the sheet: what riders wear on this bike,
    # not what the factory fitted. Community fields -- there is no factory
    # value to be right about -- on every bike.
    for i, label in enumerate(["Helmet", "Gloves", "Boots", "Goggles",
                               "Heated Vest", "Jacket"]):
        registry.add(label, "Gear and Accessories", spec_type="community", order_hint=i)

    field_count = registry.write(conn)

    # A wire colour is a colour pair from a fixed list, not free text. Marked
    # here as well as in the migration, so a database built from scratch and one
    # upgraded in place agree — a field marked only by the migration would be
    # plain text again after any reseed.
    conn.execute(
        "UPDATE spec_fields SET value_type='wire_color'"
        " WHERE LOWER(label) LIKE '%wire%' AND LOWER(label) LIKE '%color%'")

    # The octane field keeps its original key because specs and header pins
    # point at it; the questionnaire names it "Minimum Fuel Octane" and an
    # alias folds that onto the key. The label shown to readers is then set
    # here, so a fresh database and the migrated one agree.
    conn.execute(
        "UPDATE spec_fields SET label='Minimum Fuel Octane', value_type='fuel_octane',"
        " universal=1 WHERE field_key='fuel_octane_grade'")
    conn.execute(
        "UPDATE spec_fields SET value_type='ethanol', universal=1"
        " WHERE field_key='max_ethanol'")
    conn.execute(
        "UPDATE spec_fields SET universal=1 WHERE category='Gear and Accessories'")
    # A CVT scooter has no sprockets, but the belt branch carries them for the
    # bikes with pulleys. On a scooter they start offline.
    conn.executemany(
        "INSERT OR IGNORE INTO field_offline_defaults (field_key, bike_type) VALUES (?, 'Scooter')",
        [("front_sprocket",), ("rear_sprocket",)])

    users = seed_users(conn)
    stats = import_catalog(conn, registry, v1, v2,
                           cb919_doc["bike"]["model_code"])
    bike_id, spec_ids = import_cb919(conn, registry, cb919_doc, users)

    # A universal field belongs on every bike, including the ones the catalog
    # just created; the API does this at bike creation, so the seed does it
    # here for the bikes that never went through the API.
    conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " SELECT b.id, f.field_key, NULL, 'pending' FROM bikes b"
        " JOIN spec_fields f ON f.universal=1")

    conn.execute(
        "INSERT INTO bike_managers (user_id, bike_id, specialty)"
        " VALUES (?,?,?)", (users["m.alvarez"], bike_id, "Street / Sport"))

    seed_service_tasks(conn, registry, cb919_doc)
    seed_enrichment(conn, cb919_doc, bike_id, users)
    seed_queues(conn, users, bike_id, spec_ids)
    seed_garage(conn, users, bike_id)
    votes = seed_alternate_votes(conn, users)

    conn.commit()

    def count(table):
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    print("data.db rebuilt\n")
    print(f"  spec_fields      {field_count}")
    print(f"  bikes            {count('bikes')}"
          f"   (folded from {len(v1)} catalog rows"
          f", {stats['cb919_rows_dropped']} CB919 rows replaced by researched data)")
    print(f"  bike_years       {count('bike_years')}")
    print(f"    in v2 catalog  {conn.execute('SELECT COUNT(*) FROM bike_years WHERE in_v2=1').fetchone()[0]}")
    print(f"    dropped in v2  {conn.execute('SELECT COUNT(*) FROM bike_years WHERE in_v2=0').fetchone()[0]}  (see catalog_v2_dropped)")
    print(f"  bike_names       {count('bike_names')}")
    print(f"  specs            {count('specs')}")
    print(f"  spec_alternates  {count('spec_alternates')}")
    print(f"  alternate_votes  {votes}")
    print(f"  users            {count('users')}   (password: {DEV_PASSWORD!r} — dev only)")
    print(f"  value_flags      {count('value_flags')}")
    print(f"  spec_tools       {count('spec_tools')}")
    print(f"  spec_links       {count('spec_links')}")
    print(f"  service_log      {count('service_log')}")
    conn.close()


if __name__ == "__main__":
    main()
