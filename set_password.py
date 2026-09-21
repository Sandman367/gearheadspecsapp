"""Set a user's password from the command line -- for the server, where the
seeded dev accounts (password 'gearhead') must be changed before anyone else
can reach the site, and for a member who is locked out.

    py set_password.py <username>            # prompts, twice, without echo
    py set_password.py <username> --disable  # suspend the account instead

Hashes the same way registration does (PBKDF2-HMAC-SHA256, per-user salt)
and signs the user out everywhere: a session opened with the old password
should not outlive it.
"""
import getpass
import os
import secrets
import sqlite3
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from app import DB_PATH, hash_password   # noqa: E402  (the app's own hashing)


def main(argv):
    if not argv or argv[0].startswith("-"):
        raise SystemExit(__doc__)
    username = argv[0]
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT id, role, suspended FROM users WHERE username=?", (username,)).fetchone()
    if not row:
        raise SystemExit(f"no user named {username!r}")
    uid, role, suspended = row

    if "--disable" in argv:
        conn.execute("UPDATE users SET suspended=1 WHERE id=?", (uid,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        conn.commit()
        print(f"{username} ({role}) suspended and signed out everywhere")
        return

    pw = getpass.getpass(f"New password for {username} ({role}): ")
    if len(pw) < 8:
        raise SystemExit("use at least 8 characters")
    if getpass.getpass("Again: ") != pw:
        raise SystemExit("they did not match; nothing changed")
    salt = secrets.token_hex(16)
    conn.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
                 (hash_password(pw, salt), salt, uid))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.commit()
    print(f"password set for {username} ({role}); existing sessions ended")


if __name__ == "__main__":
    main(sys.argv[1:])
