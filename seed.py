import sqlite3, os

DB_PATH = "data.db"
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

conn = sqlite3.connect(DB_PATH)
conn.executescript(open("schema.sql").read())
cur = conn.cursor()

# ---- Bikes ----
cur.execute("INSERT INTO bikes (make, model, year, cc, cc_verified, bike_type) VALUES (?,?,?,?,?,?)",
            ("Honda", "CB900F2 919", 2005, 919, 1, "Street bike / Sport bike"))
cb919_id = cur.lastrowid

cur.execute("INSERT INTO bikes (make, model, year, cc, cc_verified, bike_type) VALUES (?,?,?,?,?,?)",
            ("Honda", "CR125", 2001, 125, 1, "Dirt bike / Off-road"))
cr125_id = cur.lastrowid

cur.execute("INSERT INTO bikes (make, model, year, cc, cc_verified, bike_type) VALUES (?,?,?,?,?,?)",
            ("Honda", "XR650L", 2009, 650, 1, "Dual-sport / Adventure"))
xr650_id = cur.lastrowid

# ---- Users ----
users = [
    ("M. Alvarez", "bike_manager"),
    ("J. Okafor", "bike_manager"),
    ("D. Chen", "bike_manager"),
    ("rider_kestrel99", "public"),
    ("t.moreno", "public"),
    ("cb919_dave", "public"),
    ("admin", "admin"),
]
for u, r in users:
    cur.execute("INSERT INTO users (username, role) VALUES (?,?)", (u, r))

cur.execute("INSERT INTO bike_managers (username, bike_id, specialty) VALUES (?,?,?)",
            ("M. Alvarez", cb919_id, "Street / Sport"))
cur.execute("INSERT INTO bike_managers (username, bike_id, specialty) VALUES (?,?,?)",
            ("J. Okafor", cr125_id, "Dirt / Off-road"))
cur.execute("INSERT INTO bike_managers (username, bike_id, specialty) VALUES (?,?,?)",
            ("D. Chen", xr650_id, "Dual-sport / Adventure"))

# ---- CB919 specs (from honda-browser.html's ENRICHED_OVERRIDES) ----
cb919_specs = {
    "General": [
        ("Curb Weight", "193.7 kg / 427 lb (dry)"),
        ("Wheelbase", "1461 mm / 57.5 in"),
        ("Seat Height", "800 mm / 31.5 in"),
    ],
    "Engine": [
        ("Cylinder Configuration", "Inline-4 — Outer Pair (1&4) / Inner Pair (2&3) grouping applies"),
        ("Engine Displacement", "919cc"),
        ("Bore", "71mm"),
        ("Stroke", "58mm"),
        ("Compression Ratio", "10.8:1"),
        ("Engine Oil Weight", "10W-40"),
        ("Engine Oil Volume", "3.8 L w/ filter change"),
        ("Intake Valve Clearance", "0.16 mm (cold)"),
        ("Exhaust Valve Clearance", "0.25 mm (cold)"),
        ("Ignition Timing", "ECU-controlled, non-adjustable (reference: 10° BTDC idle)"),
        ("Oil Filter Part Number", "OEM Honda 15410-MFJ-D01"),
        ("Coolant Type", "Honda HP Coolant (or equivalent silicate-free ethylene glycol)"),
        ("Coolant Capacity", "2.8 L (total system)"),
        ("Temperature Sensor Location", "Left side of cylinder head, below thermostat housing"),
        ("Fan Relay Location", "Under seat, relay box"),
        ("Fan Fuse Location", "Fuse box, under seat"),
        ("Fan Fuse Type", "Blade (mini)"),
        ("Fan Fuse Amp Rating", "15A"),
        ("Spark Plug (NGK)", None),
        ("Spark Plug Gap (mm)", None),
    ],
    "Drive": [
        ("Drive Type", "Chain and sprockets"),
        ("Chain", "530, 108 links (O-ring)"),
        ("Front Sprocket", "16T, 530 pitch"),
        ("Rear Sprocket", "43T, 530 pitch"),
        ("Front Left Caliper", "Nissin 4-piston, 296mm dual full-floating discs"),
        ("Front Right Caliper", "Nissin 4-piston, 296mm dual full-floating discs"),
        ("Front Brake Pads", "(Qty: 2 required) OEM Honda 06455-MCJ-D01"),
        ("Rear Brake Pads", "OEM Honda 43105-MCJ-D01"),
        ("Front Tire Size", "120/70ZR-17"),
        ("Front Tire Pressure", "36 PSI (cold)"),
        ("Rear Tire Size", "180/55ZR-17"),
        ("Rear Tire Pressure", "42 PSI (cold)"),
        ("Front Valve Stem Type", "TR414 (tubeless)"),
        ("Rear Valve Stem Type", "TR414 (tubeless)"),
    ],
    "Fuel and Air": [
        ("Fuel System", "PGM-FI Electronic Fuel Injection with manual enricher circuit"),
        ("Fuel Pump Relay", "Under seat, relay box, position 2"),
        ("Fuel Pump Fuse Location", "Fuse box, under seat"),
        ("Fuel Pump Fuse Type", "Blade (mini)"),
        ("Fuel Pump Fuse Amp Rating", "10A"),
        ("Tip-Over Sensor", "Under fuel tank, front-left mount"),
        ("Fuel Tank Capacity", "19 L / 5.0 gal (incl. reserve)"),
        ("Air Filter", None),
    ],
    "Controls": [
        ("Front Brake Fluid", "DOT 4"),
        ("Rear Brake Fluid", "DOT 4"),
    ],
    "Suspension": [
        ("Front Fork Type", "43mm cartridge fork, 4.7 in travel"),
        ("Rear Shock Preload Setting", "7-position adjustable, 5.0 in travel"),
    ],
    "Electrical": [
        ("Battery", "YTZ10S, 12V 8.6Ah (OEM 31500-MCJ-642AH)"),
        ("Fuse Box Location", "Under seat, left side"),
        ("Fuse Type", "Blade (standard + mini mixed)"),
        ("Starter Part Number", "OEM Honda 31200-MFJ-D01"),
        ("Starter Relay Location", "Under seat, next to fuse box"),
        ("Starter Fuse Location", "Fuse box, under seat"),
        ("Starter Fuse Type", "Blade (standard)"),
        ("Starter Fuse Amp Rating", "30A"),
        ("Starter Switch Wire Color", None),
    ],
}

spec_id_map = {}  # label -> spec_id, for CB919, to attach flags later
for cat, specs in cb919_specs.items():
    for label, val in specs:
        cur.execute("INSERT INTO bike_specs (bike_id, category, label, stock_value) VALUES (?,?,?,?)",
                    (cb919_id, cat, label, val))
        spec_id_map[label] = cur.lastrowid

# A couple of alternates for realism (mirrors honda-browser.html demo data)
def add_alt(label, text, submitted_by, votes, confirmed=0):
    cur.execute("INSERT INTO alternates (spec_id, text, submitted_by, votes, confirmed_fit) VALUES (?,?,?,?,?)",
                (spec_id_map[label], text, submitted_by, votes, confirmed))

add_alt("Front Brake Pads", "EBC FA187HH — Double-H Sintered", "cb919_dave", 12, 1)
add_alt("Engine Oil Weight", "Mobil 1 15W-50 Full Synthetic", "rider_kestrel99", 3)

# ---- CR125 (minimal, matches earlier questionnaire run) ----
cr125_specs = {
    "Engine": [
        ("Cylinder Configuration", "1 cylinder"),
        ("Premix Fuel:Oil Ratio", "32:1"),
        ("Recommended 2-Stroke Oil", "Honda HP2 or equivalent premix oil"),
        ("Transmission Oil Weight", "10W-30 (non-synthetic, no friction modifiers)"),
        ("Transmission Oil Volume", "700 ml"),
        ("Carb 1 Pilot Jet", "#42"),
        ("Carb 1 Main Jet", "#178"),
        ("Ignition Timing", "Fixed — CDI controlled, non-adjustable"),
    ],
    "Drive": [
        ("Chain", "520, 114 links"),
        ("Front Sprocket", "13T, 520 pitch"),
        ("Rear Sprocket", "50T, 520 pitch"),
        ("Front Left Caliper", "OEM Honda — Nissin 2-piston"),
        ("Front Left Brake Pads", "OEM Honda 06455-KZ3-D02"),
        ("Rear Brake Pads", "OEM Honda 43105-KZ3-D02"),
    ],
    "Suspension": [
        ("Front Shock Air Pressure", None),
    ],
    "Controls": [
        ("Front Brake Fluid", "DOT 4"),
        ("Rear Brake Fluid", "DOT 4"),
    ],
}
for cat, specs in cr125_specs.items():
    for label, val in specs:
        cur.execute("INSERT INTO bike_specs (bike_id, category, label, stock_value) VALUES (?,?,?,?)",
                    (cr125_id, cat, label, val))

# ---- XR650L: intentionally sparse — this bike's questionnaire run was left "in progress" ----
xr650_specs = {
    "Engine": [("Cylinder Configuration", "1 cylinder")],
}
for cat, specs in xr650_specs.items():
    for label, val in specs:
        cur.execute("INSERT INTO bike_specs (bike_id, category, label, stock_value) VALUES (?,?,?,?)",
                    (xr650_id, cat, label, val))

conn.commit()

# ---- Flags (matches bike-manager-dashboard.html mock data) ----
def get_spec_id(bike_id, label):
    cur.execute("SELECT id FROM bike_specs WHERE bike_id=? AND label=?", (bike_id, label))
    row = cur.fetchone()
    return row[0] if row else None

cur.execute("""INSERT INTO value_flags (spec_id, entered_by, flagged_by, reason, detail, old_value, new_value)
             VALUES (?,?,?,?,?,?,?)""",
            (get_spec_id(cb919_id, "Front Brake Pads"), "M. Alvarez", "rider_kestrel99", "incorrect",
             "Left and right pads actually wear differently on my bike — shouldn't be one shared value.",
             "(Qty: 2 required) OEM Honda 06455-MCJ-D01", None))

cur.execute("""INSERT INTO value_flags (spec_id, entered_by, flagged_by, reason, detail, old_value, new_value)
             VALUES (?,?,?,?,?,?,?)""",
            (get_spec_id(cb919_id, "Coolant Capacity"), "cb919_dave", "t.moreno", "irrelevant",
             "This value looks like it's for the wrong sub-model.", "2.8 L (total system)", None))

cur.execute("""INSERT INTO value_flags (spec_id, entered_by, flagged_by, reason, detail, old_value, new_value)
             VALUES (?,?,?,?,?,?,?)""",
            (get_spec_id(cb919_id, "Starter Switch Wire Color"), "rider_kestrel99", "cb919_dave", "other",
             "Wire color changed on later production runs — worth a note?", None, None))

# ---- Not-sure answers ----
cur.execute("INSERT INTO not_sure_answers (bike_id, question_text, submitted_by) VALUES (?,?,?)",
            (xr650_id, "Does this bike have oil filter?", "D. Chen"))
cur.execute("INSERT INTO not_sure_answers (bike_id, question_text, submitted_by) VALUES (?,?,?)",
            (xr650_id, "Is this a mechanical fuel pump?", "D. Chen"))
cur.execute("INSERT INTO not_sure_answers (bike_id, question_text, submitted_by) VALUES (?,?,?)",
            (cr125_id, "Does this bike have a Rear shock air pressure?", "J. Okafor"))
cur.execute("INSERT INTO not_sure_answers (bike_id, question_text, submitted_by) VALUES (?,?,?)",
            (cb919_id, "What ignition system does this bike have?", "M. Alvarez"))

# ---- Branch proposals ----
cur.execute("""INSERT INTO branch_proposals (field_name, category, bike_id, proposed_by, reasoning)
             VALUES (?,?,?,?,?)""",
            ("Steering Damper Preload", "Suspension", cr125_id, "J. Okafor",
             "Aftermarket steering dampers are extremely common on this bike and preload setting genuinely varies rider to rider."))
cur.execute("""INSERT INTO branch_proposals (field_name, category, bike_id, proposed_by, reasoning)
             VALUES (?,?,?,?,?)""",
            ("Sprocket Anti-Rust Coating", "Drive", xr650_id, "D. Chen",
             "Riders in coastal/salt areas care about this a lot — comes up constantly in dual-sport forums."))
cur.execute("""INSERT INTO branch_proposals (field_name, category, bike_id, proposed_by, reasoning)
             VALUES (?,?,?,?,?)""",
            ("Fork Preload Adjuster Type", "Suspension", cb919_id, "M. Alvarez", None))

# ---- Tree flags (Spec Tree structural feedback -> admin) ----
cur.execute("""INSERT INTO tree_flags (spec_id, bike_id, question_text, comment)
             VALUES (?,?,?,?)""",
            (get_spec_id(xr650_id, "Cylinder Configuration"), xr650_id, "Does this bike have oil filter?",
             "This bike actually uses an oil screen, not a cartridge filter — the questionnaire branch felt like it didn't fit either option cleanly."))

conn.commit()
conn.close()
print("Seeded database at", os.path.abspath(DB_PATH))
