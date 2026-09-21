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


def main():
    data_dir = os.environ.get("DATA_DIR") or ROOT
    os.makedirs(data_dir, exist_ok=True)
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
    import app
    app.main()


if __name__ == "__main__":
    main()
