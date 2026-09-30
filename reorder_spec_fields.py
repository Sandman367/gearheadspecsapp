"""
Put related specs next to each other within each category, once.

The fields were ordered by when they were created, so a bike sheet read
Valve Clearance -- Exhaust, then eight rows later Exhaust Shim Size, and
Carb 4 Pilot Jet after Carb 6. This lays each category out by topic:
valves with their shims, front brake parts together, carbs in order.

Nothing changes category. Each category's existing sort numbers are reused
in the new order, so no field can drift into another category's band. A
field not named here keeps its place after the named ones, in the order it
had. A field named here but missing from the database is skipped.

Also puts back three labels that had been overwritten by accident -- only
while they still read the accidental text, so a later rename stands:
  fuse_type            "1"        -> "Fuse Type"
  fan_fuse_type        "Fan"      -> "Fan Fuse Type"
  fan_fuse_amp_rating  "Fan Fuse" -> "Fan Fuse Amp Rating"

Runs once: it records itself in applied_fixes, so fields an admin moves
afterwards stay where they were put.

Run:  py reorder_spec_fields.py [path/to/data.db]
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(ROOT, "data.db")
NAME = "reorder_spec_fields_2026_09_30"

ORDER = {
    "Engine": [
        # basics
        "cylinder_configuration", "engine_displacement", "bore_x_stroke",
        "compression_ratio", "idle_speed",
        # ignition
        "ignition_timing", "ignition_order", "spark_plug_standard",
        "spark_plug_gap", "points_gap", "condenser_part_number",
        # valves, each with its shim
        "valve_clearance_intake", "intake_shim_size",
        "valve_clearance_exhaust", "exhaust_shim_size",
        # engine oil
        "engine_oil_weight", "engine_oil_volume", "oil_filter_part_number",
        "oil_filter_1_part_number", "oil_filter_2_part_number",
        "oil_change_interval", "oil_screen_location",
        # two-stroke oil
        "auto_lube_oil_type", "auto_lube_oil_tank_capacity",
        "premix_fuel_oil_ratio", "recommended_2_stroke_oil",
        "gear_oil", "gear_oil_volume",
        # cooling
        "coolant_type", "coolant_capacity", "temperature_sensor_location",
        "fan_relay_location", "fan_fuse_type", "fan_fuse_amp_rating",
        "exhaust",
    ],
    "Drive": [
        "primary_oil_weight", "primary_oil_volume",
        "transmission_oil_weight", "transmission_oil_volume",
        "primary_transmission_oil_weight", "primary_transmission_oil_volume",
        "drive_type",
        "drive_chain", "chain_length_links", "master_link_type",
        "drive_chain_slack", "front_sprocket", "rear_sprocket",
        "drive_belt", "belt_conditioner",
        "drive_shaft_oil_weight", "drive_shaft_oil_volume",
        "front_tire_size", "front_tire_pressure",
        "front_inner_tube_size_part_number", "front_valve_stem_type",
        "rear_tire_size", "rear_tire_pressure",
        "rear_inner_tube_size_part_number", "rear_valve_stem_type",
    ],
    "Brakes": [
        "front_brake_pads", "front_left_brake_pads", "front_right_brake_pads",
        "front_brake_pad_left", "front_brake_pad_right", "front_brake_shoes",
        "front_brake_rotor_size", "front_left_caliper", "front_right_caliper",
        "front_brake_master_cylinder", "front_brake_fluid", "front_brake_lever",
        "rear_brake_pads", "rear_brake_shoes", "rear_brake_rotor_size",
        "rear_brake_master_cylinder", "rear_brake_fluid", "rear_brake_lever",
    ],
    "Fuel and Air": [
        "fuel_system", "fuel_octane_grade", "fuel_additive",
        "fuel_tank_capacity", "air_filter",
        "fuel_pump_location", "fuel_pump_relay", "fuel_pump_fuse_location",
        "fuel_pump_fuse_type", "fuel_pump_fuse_amp_rating", "tip_over_sensor",
        "carb_1_main_jet", "carb_1_pilot_jet", "carb_2_main_jet", "carb_2_pilot_jet",
        "carb_3_main_jet", "carb_3_pilot_jet", "carb_4_main_jet", "carb_4_pilot_jet",
        "carb_5_main_jet", "carb_5_pilot_jet", "carb_6_main_jet", "carb_6_pilot_jet",
    ],
    "Controls": [
        "clutch_lever_freeplay", "clutch_cable", "hydraulic_clutch_fluid_type",
        "brake_pedal",
    ],
    "Suspension": [
        "front_fork_type", "front_suspension_fluids", "front_shock_oil_level",
        "front_shock_air_pressure_psi", "rear_shock_preload_setting",
        "rear_shock_air_pressure_psi",
    ],
    "Electrical": [
        "battery",
        "fuses", "fuse_box_location", "fuse_type",
        "starter_part_number", "starter_relay_location", "starter_fuse_location",
        "starter_fuse_type", "starter_fuse_amp_rating", "starter_switch_wire_color",
        "fan_fuse_location",
        "head_light_bulb", "high_beam_bulb", "low_beam_bulb", "headlight_wire_color",
        "parking_light_bulb", "tail_light_bulb", "brake_light_bulb",
        "turn_signal_light_bulb", "left_turn_signal_wire_color",
        "right_turn_signal_wire_color",
        "kill_switch_wire_color", "neutral_switch_wire_color", "kickstand_wire_color",
        "brake_light_switch", "front_brake_light_switch_wire_color",
        "rear_brake_light_switch_wire_color",
    ],
}

LABELS = [("fuse_type", "1", "Fuse Type"),
          ("fan_fuse_type", "Fan", "Fan Fuse Type"),
          ("fan_fuse_amp_rating", "Fan Fuse", "Fan Fuse Amp Rating")]


def migrate(db_path):
    if not os.path.exists(db_path):
        raise SystemExit(f"no database at {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS applied_fixes"
                     " (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)")
        if conn.execute("SELECT 1 FROM applied_fixes WHERE name=?", (NAME,)).fetchone():
            print("spec order already tidied")
            return

        moved = 0
        for category, wanted in ORDER.items():
            current = conn.execute(
                "SELECT field_key, sort_order FROM spec_fields WHERE category=?"
                " ORDER BY sort_order, field_key", (category,)).fetchall()
            if not current:
                continue
            numbers = [n for _, n in current]
            have = [k for k, _ in current]
            new = [k for k in wanted if k in have] + [k for k in have if k not in wanted]
            for key, n in zip(new, numbers):
                if dict(current)[key] != n:
                    moved += 1
                conn.execute("UPDATE spec_fields SET sort_order=? WHERE field_key=?", (n, key))

        relabelled = 0
        for key, broken, label in LABELS:
            relabelled += conn.execute(
                "UPDATE spec_fields SET label=? WHERE field_key=? AND label=?",
                (label, key, broken)).rowcount

        conn.execute("INSERT INTO applied_fixes (name) VALUES (?)", (NAME,))
        conn.commit()
        print(f"spec order tidied: {moved} field(s) moved within their category,"
              f" {relabelled} label(s) put back")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DB)
