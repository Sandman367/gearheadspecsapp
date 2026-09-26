"""Start the site on a server.

    python bootstrap.py

Makes sure there is a database where DATA_DIR says (the persistent disk on a
host that rebuilds the code directory on every deploy), then runs the
server. On a brand-new disk there is no database, so one is seeded from
schema.sql and data/ -- the same thing `py seed.py` does locally -- and the
seeded accounts' passwords are replaced: admin gets BOOTSTRAP_ADMIN_PASSWORD
from the environment (or a random one, printed once to the log), everyone
else gets a random password nobody knows. Nothing seeded can be signed into
with the dev password on a public address. From there admin restores the
real database through Admin -> Backups & Restore.
"""
import os
import secrets
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

MIGRATIONS = ["migrate_manager_tiers", "migrate_year_photos", "migrate_spec_notes",
              "migrate_field_example", "migrate_front_brake_pads",
              "migrate_value_source", "migrate_password_resets",
              "migrate_brakes_category"]


def main():
    data_dir = os.environ.get("DATA_DIR") or ROOT
    try:
        os.makedirs(data_dir, exist_ok=True)
        probe = os.path.join(data_dir, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        # No disk mounted there (yet). Run anyway, beside the code, so the
        # site is reachable -- but say loudly that nothing written survives
        # a deploy or restart until the disk exists.
        fallback = os.path.join(ROOT, "data-ephemeral")
        os.makedirs(fallback, exist_ok=True)
        print("=" * 72, flush=True)
        print(f"WARNING: DATA_DIR {data_dir!r} is not writable ({e.strerror}).", flush=True)
        print(f"         Using {fallback!r} instead -- NOT PERSISTENT. Attach a disk at", flush=True)
        print(f"         {data_dir!r} (Render: the service's Disks page) and redeploy.", flush=True)
        print("=" * 72, flush=True)
        data_dir = fallback
        os.environ["DATA_DIR"] = fallback      # app.py reads it on import
    db_path = os.path.join(data_dir, "data.db")
    if not os.path.exists(db_path):
        print(f"no database at {db_path}: seeding a fresh one", flush=True)
        import seed
        seed.main()
        from app import hash_password
        conn = sqlite3.connect(db_path)
        admin_pw = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        for uid, username, role in conn.execute("SELECT id, username, role FROM users").fetchall():
            pw = admin_pw if username == "admin" else secrets.token_urlsafe(24)
            salt = secrets.token_hex(16)
            conn.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
                         (hash_password(pw, salt), salt, uid))
        conn.execute("DELETE FROM sessions")
        conn.commit(); conn.close()
        if os.environ.get("BOOTSTRAP_ADMIN_PASSWORD"):
            print("seeded; admin password is BOOTSTRAP_ADMIN_PASSWORD from the environment;"
                  " every other seeded account has a random password", flush=True)
        else:
            print(f"seeded; admin password (shown once): {admin_pw}", flush=True)
    else:
        print(f"database: {db_path}", flush=True)

    # Schema changes that shipped after the database was first built. Each
    # is idempotent, so running them on every start costs nothing; a new
    # one is added to this list with the code that needs it.
    for name in MIGRATIONS:
        mod = __import__(name)
        mod.migrate(db_path)

    # Locked out of admin: set RESET_ADMIN_PASSWORD in the environment,
    # redeploy, sign in, then remove the variable. Applied on every start
    # while it is set, so it is loud about it.
    reset = os.environ.get("RESET_ADMIN_PASSWORD")
    if reset:
        from app import hash_password
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT id, username FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone()
        if row:
            salt = secrets.token_hex(16)
            conn.execute("UPDATE users SET password_hash=?, password_salt=?, suspended=0 WHERE id=?",
                         (hash_password(reset, salt), salt, row[0]))
            conn.execute("DELETE FROM sessions WHERE user_id=?", (row[0],))
            conn.commit()
            print(f"RESET: password for admin account {row[1]!r} set from RESET_ADMIN_PASSWORD."
                  " Remove that variable once you are back in.", flush=True)
        conn.close()
    import app
    app.main()


if __name__ == "__main__":
    main()
