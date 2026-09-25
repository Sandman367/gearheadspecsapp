"""
GearHeadSpecs — application server.

Pure Python standard library. Run with:  py app.py
Serves the REST API on /api/* and the pages in static/ on /.

Auth is a session cookie backed by the sessions table. Role gates live in the
route table itself (the `role` column), so the gate sits next to the route
rather than somewhere a reader has to go looking for.

Roles, least to most: user, manager, admin. There is no stored role for a
visitor who is not signed in — that is 'anon' on a route, and it means the
route is deliberately open. Note the default is 'anon', so a route that omits
the role is PUBLICLY READABLE; anything that writes, or reads somebody's
private data, has to name its role explicitly.
"""
import json
import os
import re
import sqlite3
import hashlib
import hmac
import secrets
import mimetypes
import threading
import urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote, quote

import questionnaire
import wire_colors
import fuel_octane

ROOT = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR: where the database and photos live. Unset, they sit beside the
# code (data.db, data/photos) as they always have. On a host whose code
# directory is rebuilt on every deploy (Render, Fly, a container) it points
# at the persistent disk, so a deploy never takes the data with it.
DATA_DIR = os.environ.get("DATA_DIR") or None
DB_PATH = os.path.join(DATA_DIR or ROOT, "data.db")
STATIC_DIR = os.path.join(ROOT, "static")

PORT = int(os.environ.get("PORT", "8420"))
# Local by default. On a server set HOST=0.0.0.0 (or put it behind a reverse
# proxy that talks to 127.0.0.1) -- see DEPLOY.md.
HOST = os.environ.get("HOST", "127.0.0.1")

# ---------------------------------------------------------------------------
# Mail
#
# Sent through Resend's HTTPS API with urllib rather than an SDK, because this
# project has no dependencies and is not about to grow one to send a handful of
# messages. With no key set nothing is sent and the message is written to the
# log instead -- so a development box works, and a deploy that has lost its key
# says so in the log rather than dropping a password reset on the floor.
# ---------------------------------------------------------------------------
MAIL_KEY = os.environ.get("RESEND_API_KEY") or None
MAIL_FROM = os.environ.get("MAIL_FROM") or "GearHeadSpecs <noreply@gearheadspecs.com>"
SITE_URL = (os.environ.get("SITE_URL") or "http://127.0.0.1:8420").rstrip("/")
RESET_HOURS = 1
SESSION_DAYS = 14
IMPERSONATE_HOURS = 4      # a test sign-in as another member lasts this long
PBKDF2_ROUNDS = 200_000
COOKIE_NAME = "ghs_session"
# While admin is signed in as somebody else for testing, their own session
# token waits in this second cookie so one click brings them back.
ADMIN_COOKIE = "ghs_admin"


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db():
    # 15 s busy timeout rather than the 5 s default: the database is in WAL
    # mode (set by seed.py / migrate_divergence_view.py), so readers never
    # block a write, and the only wait left is one writer behind another.
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def rows(cur):
    return [dict(r) for r in cur.fetchall()]


def one(cur):
    r = cur.fetchone()
    return dict(r) if r else None


class HttpError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# Passwords and sessions
# ---------------------------------------------------------------------------
def hash_password(password, salt):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), PBKDF2_ROUNDS
    ).hex()


def verify_password(password, stored_hash, salt):
    # compare_digest, not ==, so a wrong password cannot be narrowed down by
    # timing how long the comparison took.
    return hmac.compare_digest(hash_password(password, salt), stored_hash)


def create_session(conn, user_id):
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
        (token, user_id, expires.strftime("%Y-%m-%d %H:%M:%S")))
    conn.commit()
    return token


def user_for_token(conn, token):
    if not token:
        return None
    return one(conn.execute(
        "SELECT u.id, u.username, u.display_name, u.role, u.suspended, s.token"
        " FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ? AND s.expires_at > datetime('now')", (token,)))


ROLE_RANK = {"user": 0, "manager": 1, "admin": 2}


def manages_bike(conn, user, bike_id):
    """Admins can act on any bike; a manager only on bikes assigned to them.

    This is the rule that makes the flag queue meaningful — 'the person closest
    to the bike' has to actually be enforced, not just displayed.
    """
    if user["role"] == "admin":
        return True
    return conn.execute(
        "SELECT 1 FROM bike_managers WHERE user_id=? AND bike_id=?",
        (user["id"], bike_id)).fetchone() is not None


def require_manages(conn, user, bike_id):
    if not manages_bike(conn, user, bike_id):
        raise HttpError(403, "you do not manage this bike")

def forget_placements(conn, bike_id, field_key):
    """When a field leaves a bike, the manager's per-bike choices about it go
    too -- the extra headings and the header pin. Leaving them would make the
    same choices silently reappear if the field is ever added back, and
    nobody asked for that; the sheet is a fresh gap then."""
    if conn.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key=?",
                    (bike_id, field_key)).fetchone():
        return   # a year variant still carries the field here
    conn.execute("DELETE FROM bike_spec_categories WHERE bike_id=? AND field_key=?",
                 (bike_id, field_key))
    conn.execute("DELETE FROM bike_header_specs WHERE bike_id=? AND field_key=?",
                 (bike_id, field_key))



def check_value(conn, spec_id, value):
    """Normalise a value against its field's type, or refuse it.

    Every path that writes a value goes through here — the manager's edit, a
    rider's submission, an alternate — so a wire colour cannot arrive as free
    text through whichever door happens to be unguarded.
    """
    row = conn.execute(
        "SELECT f.value_type, f.label FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key WHERE s.id=?",
        (spec_id,)).fetchone()
    if not row or row["value_type"] == "text":
        return value
    if value is None or not str(value).strip():
        return value
    try:
        if row["value_type"] == "wire_color":
            # normalise_set, not normalise: a spec can be several wires — a
            # kickstand switch has two — and one wire is simply a list of one.
            return wire_colors.normalise_set(value)
        if row["value_type"] == "fuel_octane":
            return fuel_octane.normalise_octane(value)
        if row["value_type"] == "ethanol":
            return fuel_octane.normalise_ethanol(value)
    except (wire_colors.WireColorError, fuel_octane.FuelValueError) as e:
        raise HttpError(400, f"{row['label']}: {e}")
    return value


# What a field's values ARE: typed text, or one of the closed vocabularies.
# Chosen when a field is created and changeable afterwards; "text" is the
# default, so a field nobody typed a choice for is a text box.
VALUE_TYPES = ("text", "wire_color", "fuel_octane", "ethanol")


def value_type_from(body):
    vt = (body.get("value_type") or "text").strip()
    if vt not in VALUE_TYPES:
        raise HttpError(400, "value_type must be one of: " + ", ".join(VALUE_TYPES))
    return vt


def normalise_for_type(value_type, value):
    """The canonical form of `value` under a value type, or raise the
    vocabulary's own error. 'text' accepts anything."""
    if value_type == "wire_color":
        return wire_colors.normalise_set(value)
    if value_type == "fuel_octane":
        return fuel_octane.normalise_octane(value)
    if value_type == "ethanol":
        return fuel_octane.normalise_ethanol(value)
    return value


def refuse_if_paused(conn, user, spec_id):
    """Offline means offline: no votes, flags or submissions while it is down.

    Hiding the value without closing these would leave riders voting on a
    number they cannot see. The bike's manager is exempt — they took it offline
    to work on it, and the fix is a write.
    """
    row = conn.execute(
        "SELECT s.paused, s.bike_id, f.label FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key WHERE s.id=?",
        (spec_id,)).fetchone()
    if not row or not row["paused"]:
        return
    if user and manages_bike(conn, user, row["bike_id"]):
        return
    raise HttpError(409, f"\"{row['label']}\" is offline while its manager "
                         f"reviews it — it will be back shortly")


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------
ROUTES = []


def route(method, pattern, role="anon"):
    """role: anon | user | manager | admin

    anon   — no session needed, including visitors who are not signed in
    user   — any signed-in account
    manager/admin — minimum role rank
    """
    compiled = re.compile("^" + pattern + "$")

    def wrap(fn):
        ROUTES.append((method, compiled, fn, role))
        return fn
    return wrap


class Ctx:
    def __init__(self, conn, user, params, query, body):
        self.conn = conn
        self.user = user
        self.params = params
        self.query = query
        self.body = body

    def arg(self, name, default=None):
        v = self.query.get(name)
        return v[0] if v else default

    def field(self, name, required=True, default=None):
        v = self.body.get(name, default)
        if required and (v is None or (isinstance(v, str) and not v.strip())):
            raise HttpError(400, f"missing field: {name}")
        return v.strip() if isinstance(v, str) else v


# ===========================================================================
# AUTH
# ===========================================================================
@route("POST", r"/api/auth/login")
def login(ctx):
    username = ctx.field("username").strip()
    password = ctx.field("password")
    u = one(ctx.conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)))
    if not u:
        # "Admin" for "admin": forgive the case when it names exactly one
        # account. Two accounts differing only by case stay strict.
        near = rows(ctx.conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)))
        if len(near) == 1:
            u = near[0]
    # Same error for unknown user and wrong password: telling them apart is a
    # free list of valid usernames.
    if not u or not verify_password(password, u["password_hash"], u["password_salt"]):
        raise HttpError(401, "invalid username or password")
    if u["suspended"]:
        raise HttpError(403, "this account is suspended")
    token = create_session(ctx.conn, u["id"])
    return {"user": {"id": u["id"], "username": u["username"],
                     "display_name": u["display_name"], "role": u["role"]},
            "_set_cookie": token}


# Rules kept next to the route that enforces them, and quoted verbatim by the
# register page so the two can never tell the reader different things.
USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN = 10
USERNAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
# Loose on purpose: one @, something either side, a dot in the domain. The
# only way to really validate an address is to send to it, and this does not.
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX = 254


@route("POST", r"/api/auth/register")
def register(ctx):
    """Open registration. The account always comes out as a plain reader.

    `role` is never read from the body. A new account is 'user' and nothing
    else: manager is something an admin grants by assigning a bike, and admin
    is not grantable through the API at all. Accepting a role here would let
    anyone who can reach the endpoint hand themselves the platform.

    A taken username has to be reported as taken -- the person needs to pick
    another one -- so this endpoint can confirm whether a name exists, unlike
    login, which deliberately cannot. That is inherent to registration rather
    than an oversight.
    """
    username = ctx.field("username")
    password = ctx.field("password")
    email = ctx.field("email").lower()
    display = (ctx.body.get("display_name") or "").strip() or None

    if len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
        raise HttpError(400, "that does not look like an email address")

    if not USERNAME_MIN <= len(username) <= USERNAME_MAX:
        raise HttpError(400, f"username must be {USERNAME_MIN}-{USERNAME_MAX}"
                             f" characters")
    if not USERNAME_RE.match(username):
        raise HttpError(400, "username can use letters, numbers, dot, dash and"
                             " underscore, and must start with a letter or number")
    if len(password) < PASSWORD_MIN:
        raise HttpError(400, f"password must be at least {PASSWORD_MIN} characters")
    # The two most common passwords on any site are the site's own name and the
    # username. Refusing them costs nothing and removes the easiest guesses.
    if password.lower() in (username.lower(), "gearheadspecs", "gearhead"):
        raise HttpError(400, "pick a password that is not your username or the"
                             " site's name")
    if display and len(display) > 60:
        raise HttpError(400, "display name must be 60 characters or fewer")

    if ctx.conn.execute("SELECT 1 FROM users WHERE username = ?",
                        (username,)).fetchone():
        raise HttpError(409, f"the username {username!r} is taken")
    # Same reasoning as the username: the person has to be told, so they can
    # sign in to the account they already have instead of making a second one.
    if ctx.conn.execute("SELECT 1 FROM users WHERE email = ? COLLATE NOCASE",
                        (email,)).fetchone():
        raise HttpError(409, "an account with that email already exists — sign in instead")

    salt = secrets.token_hex(16)
    cur = ctx.conn.execute(
        "INSERT INTO users (username, display_name, role, password_hash,"
        " password_salt, email) VALUES (?,?,'user',?,?,?)",
        (username, display, hash_password(password, salt), salt, email))
    uid = cur.lastrowid
    ctx.conn.commit()

    # Signed in on the way out: making somebody type the same credentials again
    # immediately after choosing them is friction with nothing behind it.
    token = create_session(ctx.conn, uid)
    return {"user": {"id": uid, "username": username,
                     "display_name": display, "role": "user"},
            "_set_cookie": token}


@route("POST", r"/api/auth/logout", role="user")
def logout(ctx):
    ctx.conn.execute("DELETE FROM sessions WHERE user_id = ?", (ctx.user["id"],))
    ctx.conn.commit()
    return {"ok": True, "_clear_cookie": True}


@route("GET", r"/api/auth/me")
def me(ctx):
    # Whether the site can send mail at all. A page must not offer a password
    # reset it cannot deliver: with no key the link would promise an email
    # that never arrives, and write the token into the server log instead.
    if not ctx.user:
        return {"user": None, "mail": bool(MAIL_KEY)}
    managed = rows(ctx.conn.execute(
        "SELECT bike_id FROM bike_managers WHERE user_id=?", (ctx.user["id"],)))
    public = {k: v for k, v in ctx.user.items() if k != "token"}
    # Admin signed in as somebody else for testing: the page shows a banner
    # and a way back. Only claimed when the waiting token really is a live
    # admin session -- a stale cookie is not a way back.
    testing = None
    if getattr(ctx, "admin_token", None):
        back = user_for_token(ctx.conn, ctx.admin_token)
        if back and back["role"] == "admin" and back["id"] != ctx.user["id"]:
            testing = {"admin": back["username"]}
    return {"user": public, "manages": [m["bike_id"] for m in managed],
            "unread": _unread_counts(ctx.conn, ctx.user), "testing_as": testing,
            "mail": bool(MAIL_KEY)}


# ===========================================================================
# CATALOG / BROWSE
# ===========================================================================
@route("GET", r"/api/catalog/filters")
def catalog_filters(ctx):
    """Options for the make / year / model selects, narrowed by whatever is
    already chosen so the dropdowns cannot offer a combination with no rows."""
    make = ctx.arg("make")
    year = ctx.arg("year")
    model = ctx.arg("model")

    where, args = [], []
    if make:
        where.append("b.make = ?"); args.append(make)
    if year:
        where.append("y.year = ?"); args.append(year)
    if model:
        where.append("b.model_code = ?"); args.append(model)
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    def distinct(col, extra_where=None):
        w, a = list(where), list(args)
        if extra_where:
            # Drop the constraint on the column being listed, so choosing a
            # model does not reduce the model list to just that model.
            w = [x for x in w if not x.startswith(extra_where)]
            a = [args[i] for i, x in enumerate(where) if not x.startswith(extra_where)]
        c = (" WHERE " + " AND ".join(w)) if w else ""
        return [r[0] for r in ctx.conn.execute(
            f"SELECT DISTINCT {col} FROM bike_years y"
            f" JOIN bikes b ON b.id = y.bike_id{c}"
            f" ORDER BY {col}", a)]

    # Models are ordered by engine size, not alphabetically — a rider scanning
    # for "something around 600cc" gets a useful ordering, where an alphabetical
    # list interleaves a 50cc scooter with a litre bike.
    #
    # The cc is parsed out of the Engine Displacement spec ("919cc" -> 919)
    # rather than stored separately, so it cannot fall out of step with the
    # value shown on the page. Models with no displacement recorded sort last
    # instead of pretending to be 0cc.
    mw, ma = list(where), list(args)
    mw = [x for x in mw if not x.startswith("b.model_code")]
    ma = [args[i] for i, x in enumerate(where) if not x.startswith("b.model_code")]
    mc = (" WHERE " + " AND ".join(mw)) if mw else ""
    models = rows(ctx.conn.execute(
        f"SELECT b.model_code,"
        f"       MAX(CAST(NULLIF(REPLACE(IFNULL(s.value,''),'cc',''),'') AS INTEGER)) AS cc"
        f" FROM bike_years y"
        f" JOIN bikes b ON b.id = y.bike_id"
        f" LEFT JOIN specs s ON s.bike_id = b.id AND s.field_key = 'engine_displacement'"
        f"{mc}"
        f" GROUP BY b.model_code"
        f" ORDER BY (cc IS NULL), cc, b.model_code", ma))

    # What the reader calls it. A Harley's model code is a factory code --
    # FLFBS, FXLRS -- and nobody shops for those; the bike's name is "Fat
    # Boy 114". The option shows the name and keeps the code beside it,
    # unless the name already carries it (Honda's CB919 and most others),
    # in which case the name alone is the label.
    names = {}
    for r in ctx.conn.execute(
            f"SELECT b.model_code, b.make, n.name, MIN(b.year_start) AS y"
            f" FROM bike_years y2"
            f" JOIN bikes b ON b.id = y2.bike_id"
            f" JOIN bike_names n ON n.bike_id = b.id AND n.is_primary = 1"
            f"{mc.replace(' y.', ' y2.')}"
            f" GROUP BY b.id ORDER BY b.model_code, y", ma):
        code, make, name = r[0], r[1], r[2]
        short = name[len(make) + 1:] if name.startswith(make + " ") else name
        names.setdefault(code, [])
        if short not in names[code]:
            names[code].append(short)
    for m in models:
        got = names.get(m["model_code"], [])
        code = m["model_code"]
        label = " / ".join(got[:2]) + (" …" if len(got) > 2 else "")
        if len(label) > 46:                      # a dropdown row, not a paragraph
            label = label[:45].rsplit(" ", 1)[0] + " …"
        if not label or label == code:
            m["label"] = code
        elif code.lower() in label.lower() or len(label) + len(code) > 52:
            m["label"] = label
        else:
            m["label"] = f"{label} · {code}"

    return {
        "makes":  distinct("b.make", "b.make"),
        "years":  distinct("y.year", "y.year"),
        "models": models,
    }


@route("GET", r"/api/bikes")
def list_bikes(ctx):
    """Search hits bike_names, so 'Hornet 900' and 'CB919' find the same
    machine — the whole point of names being data rather than identity."""
    make, year, model = ctx.arg("make"), ctx.arg("year"), ctx.arg("model")
    q = ctx.arg("q")
    limit = min(int(ctx.arg("limit", "200")), 1000)

    where, args = [], []
    if make:
        where.append("d.make = ?"); args.append(make)
    if model:
        where.append("d.model_code = ?"); args.append(model)
    if year:
        where.append("EXISTS (SELECT 1 FROM bike_years y WHERE y.bike_id=d.bike_id AND y.year=?)")
        args.append(year)
    if q:
        where.append(
            "(EXISTS (SELECT 1 FROM bike_names n WHERE n.bike_id=d.bike_id"
            "         AND n.name LIKE ?) OR d.model_code LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    clause = (" WHERE " + " AND ".join(where)) if where else ""

    # The catalog listing is the public view of every bike, so it reports the
    # public numbers even to a manager: this is the browse page, not their
    # dashboard, and the counts should match what the spec sheet will show.
    result = rows(ctx.conn.execute(
        "SELECT d.*, p.fields_triggered_public AS fields_triggered,"
        "       p.specs_filled_public AS specs_filled,"
        "       p.specs_needed_public AS specs_needed"
        " FROM bike_display d"
        " JOIN bike_spec_progress p ON p.bike_id = d.bike_id"
        f"{clause} ORDER BY d.model_code, d.year_start LIMIT ?", args + [limit]))
    total = ctx.conn.execute(
        f"SELECT COUNT(*) FROM bike_display d{clause}", args).fetchone()[0]
    return {"bikes": result, "total": total}


@route("GET", r"/api/stats")
def site_stats(ctx):
    """The three numbers on the browse page. Public, so they count what the
    public can see: offline specs are off the sheet and off the count."""
    c = ctx.conn
    return {
        "bikes": c.execute("SELECT COUNT(*) FROM bikes").fetchone()[0],
        "specs": c.execute("SELECT COUNT(*) FROM specs WHERE paused = 0").fetchone()[0],
        "values": c.execute("SELECT COUNT(*) FROM specs WHERE paused = 0"
                            " AND value IS NOT NULL AND TRIM(value) <> ''").fetchone()[0],
    }


@route("GET", r"/api/bikes/(\d+)")
def get_bike(ctx):
    bike_id = int(ctx.params[0])
    # The header counts have to agree with the rows underneath them. Whoever
    # looks after the bike sees its offline specs, so they are counted for them
    # and not for anybody else.
    mine = bool(ctx.user) and manages_bike(ctx.conn, ctx.user, bike_id)
    cols = ("p.fields_triggered, p.specs_filled, p.specs_needed" if mine else
            "p.fields_triggered_public AS fields_triggered,"
            " p.specs_filled_public AS specs_filled,"
            " p.specs_needed_public AS specs_needed")
    bike = one(ctx.conn.execute(
        f"SELECT d.*, {cols}, p.specs_offline, b.split_from_bike_id, b.split_at_year"
        " FROM bike_display d JOIN bike_spec_progress p ON p.bike_id=d.bike_id"
        " JOIN bikes b ON b.id=d.bike_id"
        " WHERE d.bike_id = ?", (bike_id,)))
    if not bike:
        raise HttpError(404, "bike not found")
    if not mine:
        bike["specs_offline"] = 0
    bike["names"] = rows(ctx.conn.execute(
        "SELECT name, market, is_primary FROM bike_names WHERE bike_id=?"
        " ORDER BY is_primary DESC, name", (bike_id,)))
    bike["photo"], bike["year_photos"] = photo_of(ctx.conn, bike_id)
    bike["years"] = rows(ctx.conn.execute(
        "SELECT id AS bike_year_id, year, in_v2 FROM bike_years"
        " WHERE bike_id=? ORDER BY year", (bike_id,)))
    bike["managers"] = rows(ctx.conn.execute(
        "SELECT u.id, u.username, u.display_name, u.role, m.specialty, m.created_at AS since"
        " FROM bike_managers m JOIN users u ON u.id = m.user_id"
        " WHERE m.bike_id=? ORDER BY m.created_at, m.id", (bike_id,)))
    lead = one(ctx.conn.execute(
        "SELECT b.lead_manager_id AS id, b.lead_since AS since, u.username, u.retired_tier"
        " FROM bikes b LEFT JOIN users u ON u.id = b.lead_manager_id WHERE b.id=?", (bike_id,)))
    for m in bike["managers"]:
        st = manager_standing(ctx.conn, m["id"])
        m["tier"], m["founder"] = st["tier"], st["founder"]
        m["lead"] = lead is not None and m["id"] == lead["id"]
    # the lead manager stays named even after handing the bike on
    bike["lead_manager"] = None
    if lead and lead["id"]:
        st = manager_standing(ctx.conn, lead["id"])
        bike["lead_manager"] = {"id": lead["id"], "username": lead["username"], "since": lead["since"],
                                "tier": st["tier"], "founder": st["founder"],
                                "retired": st["retired"], "current": any(m["id"] == lead["id"] for m in bike["managers"])}
    return bike


@route("GET", r"/api/fuel-octane")
def fuel_octane_vocabulary(ctx):
    """Pump grades, their cross-region equivalents with provenance, and the
    ethanol ceilings. The equivalents are looked up, never computed: AKI cannot
    be derived from RON without MON, which no manual publishes."""
    return fuel_octane.vocabulary()


@route("GET", r"/api/wire-colors")
def wire_color_vocabulary(ctx):
    """The colour list, and the abbreviations for one make.

    ?make= because the same stored colour is written differently depending on
    whose diagram the rider is holding: Honda prints Bu for blue, Kawasaki BL,
    Harley BE. The value never changes; only the label does.
    """
    return wire_colors.vocabulary(ctx.arg("make", "") or None)


@route("GET", r"/api/bikes/(\d+)/specs")
def get_bike_specs(ctx):
    bike_id = int(ctx.params[0])
    uid = ctx.user["id"] if ctx.user else -1
    spec_rows = rows(ctx.conn.execute(
        # open_flags counts only flags against the STOCK value: a flag carrying
        # an alternate_id belongs to that alternate's line, not to the spec.
        "SELECT s.id, s.field_key, s.value, s.confidence, s.tools,"
        "       f.label, f.category, f.sort_order,"
        # Effective type: a manager's per-bike override wins over the
        # platform default, so one bike can call a field Fixed without
        # changing that field for the other 259.
        "       COALESCE(s.spec_type, f.spec_type) AS spec_type,"
        "       (s.spec_type IS NOT NULL) AS type_overridden,"
        "       f.spec_type AS field_spec_type,"
        "       u.username AS entered_by_username, s.entered_by, u.role AS entered_by_role,"
        "       u.retired_tier AS entered_by_retired, u.founder AS entered_by_founder,"
        # Where it came from and when, for every value: a username, or the
        # import that seeded it. The page promises both, so both are sent.
        "       s.value_source,"
        "       COALESCE(s.updated_at, s.created_at) AS value_at,"
        "       vc.votes, rc.requests,"
        "       EXISTS(SELECT 1 FROM spec_votes v"
        "              WHERE v.spec_id=s.id AND v.user_id=?) AS my_vote,"
        "       EXISTS(SELECT 1 FROM spec_requests r"
        "              WHERE r.spec_id=s.id AND r.user_id=?) AS my_request,"
        # Whether YOU have an open flag on the stock value — the alternates
        # carry the same field, and without it the page cannot show a flag you
        # already raised as raised.
        # The flag's id rather than a yes/no, so "flagged by mistake" on the
        # page can withdraw exactly that flag. NULL when there is none.
        "       (SELECT vf.id FROM value_flags vf"
        "         WHERE vf.spec_id=s.id AND vf.alternate_id IS NULL"
        "           AND vf.flagged_by=? AND vf.status='open' LIMIT 1) AS my_flag,"
        "       (SELECT COUNT(*) FROM value_flags vf"
        "         WHERE vf.spec_id = s.id AND vf.alternate_id IS NULL"
        "           AND vf.status='open') AS open_flags,"
        # Lifted into this bike's hero by its manager. General is in the
        # hero on every bike regardless; this is the per-bike addition.
        # in_header means "pinned": a non-General field lifted into the hero.
        # header_hidden means a General field this bike's manager took OUT.
        "       EXISTS(SELECT 1 FROM bike_header_specs h"
        "              WHERE h.bike_id = s.bike_id"
        "                AND h.field_key = s.field_key"
        "                AND h.hidden = 0) AS in_header,"
        "       EXISTS(SELECT 1 FROM bike_header_specs h"
        "              WHERE h.bike_id = s.bike_id"
        "                AND h.field_key = s.field_key"
        "                AND h.hidden = 1) AS header_hidden,"
        "       COALESCE((SELECT h.hide_below FROM bike_header_specs h"
        "                  WHERE h.bike_id = s.bike_id"
        "                    AND h.field_key = s.field_key), 0) AS header_only,"
        "       s.paused, s.paused_at, pu.username AS paused_by_username,"
        "       f.value_type, s.year_from, s.year_to,"
        # The other headings this bike's manager put the field under, and
        # whether they took it out of its home heading on this bike.
        "       (SELECT GROUP_CONCAT(c.category, '|') FROM bike_spec_categories c"
        "         WHERE c.bike_id = s.bike_id AND c.field_key = s.field_key"
        "           AND c.shown = 1) AS also_in,"
        "       EXISTS(SELECT 1 FROM bike_spec_categories c"
        "         WHERE c.bike_id = s.bike_id AND c.field_key = s.field_key"
        "           AND c.shown = 0) AS home_hidden_bike,"
        # ...and the same, set by admin for every bike.
        "       (SELECT GROUP_CONCAT(g.category, '|') FROM spec_field_categories g"
        "         WHERE g.field_key = s.field_key AND g.shown = 1) AS also_in_site,"
        "       EXISTS(SELECT 1 FROM spec_field_categories g"
        "         WHERE g.field_key = s.field_key AND g.shown = 0) AS home_hidden_site,"
        # How many rows this field has on this bike. One is the ordinary case;
        # more means the value changed partway through the bike's life and the
        # page has to say so rather than pick one.
        "       (SELECT COUNT(*) FROM specs v WHERE v.bike_id = s.bike_id"
        "          AND v.field_key = s.field_key) AS variant_count,"
        # The manager's note on this bike, and admin's note on the field
        # itself, which every bike carrying the field shows.
        "       (SELECT n.body FROM spec_notes n WHERE n.bike_id = s.bike_id"
        "          AND n.field_key = s.field_key) AS note,"
        "       (SELECT u.username FROM spec_notes n LEFT JOIN users u ON u.id = n.written_by"
        "         WHERE n.bike_id = s.bike_id AND n.field_key = s.field_key) AS note_by,"
        "       f.example,"
        "       (SELECT n.body FROM spec_notes n WHERE n.bike_id IS NULL"
        "          AND n.field_key = s.field_key) AS site_note,"
        "       (SELECT u.username FROM spec_notes n LEFT JOIN users u ON u.id = n.written_by"
        "         WHERE n.bike_id IS NULL AND n.field_key = s.field_key) AS site_note_by"
        " FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key"
        " JOIN spec_vote_counts vc ON vc.spec_id = s.id"
        " JOIN spec_request_counts rc ON rc.spec_id = s.id"
        " LEFT JOIN users u ON u.id = s.entered_by"
        " LEFT JOIN users pu ON pu.id = s.paused_by"
        " WHERE s.bike_id = ?"
        # Variants of one field sit together, earliest year first, so the page
        # can group consecutive rows without a second pass.
        " ORDER BY f.sort_order, f.label, s.year_from", (uid, uid, uid, bike_id)))

    alts = rows(ctx.conn.execute(
        "SELECT a.id, a.spec_id, a.text, a.confirmed_fit, a.paused,"
        "       c.votes, u.username AS submitted_by_username,"
        "       EXISTS(SELECT 1 FROM alternate_votes v"
        "              WHERE v.alternate_id=a.id AND v.user_id=?) AS my_vote,"
        "       (SELECT vf.id FROM value_flags vf"
        "         WHERE vf.alternate_id=a.id AND vf.flagged_by=?"
        "           AND vf.status='open' LIMIT 1) AS my_flag,"
        "       (SELECT COUNT(*) FROM value_flags vf"
        "         WHERE vf.alternate_id=a.id AND vf.status='open') AS flags"
        " FROM spec_alternates a"
        " JOIN alternate_vote_counts c ON c.alternate_id = a.id"
        " LEFT JOIN users u ON u.id = a.submitted_by"
        " JOIN specs s ON s.id = a.spec_id"
        " WHERE s.bike_id = ? ORDER BY c.votes DESC, a.id", (uid, uid, bike_id)))

    by_spec = {}
    for a in alts:
        by_spec.setdefault(a["spec_id"], []).append(a)

    # Whoever looks after this bike keeps seeing everything — they cannot judge
    # a value they have taken offline if the page hides it from them too.
    manages = bool(ctx.user) and manages_bike(ctx.conn, ctx.user, bike_id)

    categories, unplaced = [], []
    for s in spec_rows:
        # Offline means gone from the sheet, not greyed out on it. A row that
        # announces a field while withholding its value still tells a rider the
        # bike has that spec, and the whole point of taking one offline is that
        # it should not be read at all until it has been checked.
        if s["paused"] and not manages:
            continue
        # An alternate taken down is gone for a reader, the same as a spec
        # taken offline. Its manager keeps seeing it -- they have to, to put
        # it back -- marked as hidden.
        s["alternates"] = [a for a in by_spec.get(s["id"], []) if manages or not a["paused"]]
        # Where this spec shows on this bike. Its home heading, unless taken
        # out of it (on this bike, or site-wide), plus every extra heading.
        # `lead` is the heading that carries the row; the rest mirror it.
        # also_in_site and home_locked are the part a bike's manager cannot
        # change: admin decided those for every bike.
        order = lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99
        s["also_in_site"] = sorted((c for c in (s["also_in_site"] or "").split("|") if c), key=order)
        mine = [c for c in (s["also_in"] or "").split("|") if c]
        extras = sorted((set(mine) | set(s["also_in_site"])) - {s["category"]}, key=order)
        s["home_shown"] = not (s["home_hidden_bike"] or s["home_hidden_site"])
        s["home_locked"] = bool(s["home_hidden_site"])
        shown = ([s["category"]] if s["home_shown"] else []) + extras
        s["lead"] = shown[0] if shown else None
        s["also_in"] = shown[1:]
        # No heading at all: the spec is in the header (if pinned there, or
        # General) and nowhere else. Its manager still gets it, in a section of
        # its own at the end, because a spec nobody can reach cannot be fixed.
        if not shown:
            unplaced.append(s)
            continue
        if not categories or categories[-1]["name"] != s["lead"]:
            categories.append({"name": s["lead"], "specs": [], "echoes": []})
        categories[-1]["specs"].append(s)
    # Skipping rows can empty a category. An empty heading on the page, and an
    # entry in the jump-to sidebar that scrolls to nothing, would both be
    # artefacts of hiding rather than anything about the bike.
    # Rows arrive in home order, but a spec taken out of its home leads
    # under another heading, so the same heading can open twice; fold them.
    merged = {}
    for c in categories:
        if c["name"] in merged:
            merged[c["name"]]["specs"].extend(c["specs"])
        else:
            merged[c["name"]] = c
    categories = [c for c in merged.values() if c["specs"]]

    # A spec shown under more than one heading mirrors under the others,
    # after that heading's own specs. A heading with nothing of its own but
    # a mirror still gets printed.
    echoed = [s for c in categories for s in c["specs"] if s["also_in"]]
    by_name = {c["name"]: c for c in categories}
    for s in echoed:
        for name in s["also_in"]:
            if name not in by_name:
                by_name[name] = {"name": name, "specs": [], "echoes": []}
                categories.append(by_name[name])
            by_name[name]["echoes"].append(s)
    order = {n: i for i, n in enumerate(CATEGORY_ORDER)}
    categories.sort(key=lambda c: order.get(c["name"], 99))
    return {"categories": categories, "unplaced": unplaced}


# ===========================================================================
# COMMUNITY: alternates, votes, flags
# ===========================================================================
@route("POST", r"/api/specs/(\d+)/alternates", role="user")
def add_alternate(ctx):
    spec_id = int(ctx.params[0])
    refuse_if_paused(ctx.conn, ctx.user, spec_id)
    spec = one(ctx.conn.execute(
        "SELECT s.id, s.bike_id, COALESCE(s.spec_type, f.spec_type) AS spec_type"
        " FROM specs s"
        " JOIN spec_fields f ON f.field_key=s.field_key WHERE s.id=?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    # A 'fixed' field has one correct answer. Accepting alternates there would
    # invite a vote on a fact.
    if spec["spec_type"] == "fixed":
        raise HttpError(400, "this field has a single correct value — "
                             "flag it as incorrect instead of proposing an alternate")
    cur = ctx.conn.execute(
        "INSERT INTO spec_alternates (spec_id, text, submitted_by) VALUES (?,?,?)",
        (spec_id, check_value(ctx.conn, spec_id, ctx.field("text")),
         ctx.user["id"]))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


@route("POST", r"/api/specs/(\d+)/value", role="user")
def submit_spec_value(ctx):
    """Anyone signed in can fill an EMPTY spec.

    This is the point of a community database: a rider who knows the battery
    their bike takes should be able to say so, without waiting for the assigned
    manager to get to it.

    Only a gap, though. Filling a blank is additive — nothing is lost if it
    turns out wrong, and the manager sees it for review. Overwriting a value
    somebody already sourced is destructive and stays a flag, so that the
    existing value survives until a human decides.

    The value lands as 'pending': sourced by a person, not yet confirmed. It
    shows on the page as Unsourced, which is honest, and it sits in the
    manager's review queue until they confirm or correct it.
    """
    spec_id = int(ctx.params[0])
    refuse_if_paused(ctx.conn, ctx.user, spec_id)
    spec = one(ctx.conn.execute(
        "SELECT s.*, f.label, COALESCE(s.spec_type, f.spec_type) AS effective_type"
        " FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key WHERE s.id = ?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    if spec["value"] not in (None, ""):
        raise HttpError(409, "this spec already has a value — flag it as "
                             "incorrect, or suggest an alternate, instead of "
                             "overwriting it")
    # A spec the manager has explicitly tagged Fixed is closed to
    # contributions: one correct value, set by the rider who maintains the bike.
    #
    # Note this tests s.spec_type — the manager's per-bike tag — NOT the
    # effective type. Almost every field in the tree defaults to 'fixed'
    # (2,927 of 2,932 rows), so honouring the inherited default here would shut
    # the community out of ~1,950 empty specs and cancel the "anyone can fill a
    # gap" behaviour entirely. The inherited default already does its job by
    # refusing alternates; closing a spec to values is a deliberate act by the
    # person who maintains that particular bike.
    #
    # The manager is not blocked by their own tag — it is how they claim a
    # spec, not how they lock themselves out of it.
    if (spec["spec_type"] == "fixed"
            and not manages_bike(ctx.conn, ctx.user, spec["bike_id"])):
        raise HttpError(403, "this spec is marked Fixed — only the rider who "
                             "maintains this bike sets its value. Flag it if you "
                             "think the value is wrong.")
    value = check_value(ctx.conn, spec_id, ctx.field("value"))
    if len(value) > 500:
        raise HttpError(400, "value is limited to 500 characters")

    cur = ctx.conn.execute(
        "UPDATE specs SET value=?, confidence='pending', value_source=NULL, entered_by=?,"
        " updated_at=datetime('now')"
        # Re-check emptiness in the UPDATE itself: two people filling the same
        # blank at once would otherwise have the second silently overwrite the
        # first, which is the destructive case this endpoint refuses.
        " WHERE id=? AND (value IS NULL OR value='')",
        (value, ctx.user["id"], spec_id))
    if not cur.rowcount:
        ctx.conn.rollback()
        raise HttpError(409, "somebody filled this in first — reload to see it")
    ctx.conn.commit()
    return {"ok": True, "value": value, "confidence": "pending"}


@route("PATCH", r"/api/specs/(\d+)", role="manager")
def edit_spec(ctx):
    """The manager's direct edit: confirm a community value, or correct it.

    Confirming means raising the confidence off 'pending', which is also what
    takes it out of the review queue — the queue is derived from confidence
    rather than a separate flag that could drift out of step with it.
    """
    spec_id = int(ctx.params[0])
    spec = one(ctx.conn.execute("SELECT * FROM specs WHERE id=?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    require_manages(ctx.conn, ctx.user, spec["bike_id"])

    confidence = ctx.body.get("confidence", spec["confidence"])
    if confidence not in ("confirmed", "mfr", "pending"):
        raise HttpError(400, "unknown confidence")
    value = check_value(ctx.conn, spec_id, ctx.body.get("value", spec["value"]))

    # entered_by means "who supplied this value", so it only moves when the
    # value actually changes. Confirming somebody else's contribution must not
    # quietly reassign it to the confirmer — that strips the credit from the
    # rider who did the work and shifts it onto the manager's profile stats.
    changed = value != spec["value"]
    entered_by = ctx.user["id"] if changed else spec["entered_by"]
    # Where the value came from moves with the credit, for the same reason.
    # A manager confirming a seeded value without changing it has not sourced
    # it; clearing this would leave the row with no author AND no origin,
    # which is the unattributed number this column exists to prevent.
    source = None if changed else spec["value_source"]

    ctx.conn.execute(
        "UPDATE specs SET value=?, confidence=?, value_source=?, entered_by=?,"
        " updated_at=datetime('now') WHERE id=?",
        (value, confidence, source, entered_by, spec_id))
    ctx.conn.commit()
    return {"ok": True, "value_changed": changed}


@route("PATCH", r"/api/specs/(\d+)/type", role="manager")
def set_spec_type(ctx):
    """Mark one spec on your bike as Fixed — or clear the mark.

    Fixed means "one correct value, and I am the one who sets it": no
    alternates, no community value submissions. That is a judgement about a
    particular machine, so it is stored per bike. Setting it on spec_fields
    would change the field for every bike in the catalog, which is admin's
    call, not a manager's.

    spec_type = null clears the override and the field falls back to whatever
    the Spec Tree says, rather than being stuck at the manager's last choice.
    """
    spec_id = int(ctx.params[0])
    spec = one(ctx.conn.execute(
        "SELECT s.bike_id, s.field_key, f.spec_type AS field_type,"
        "       COALESCE(s.spec_type, f.spec_type) AS effective_type"
        " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE s.id = ?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    require_manages(ctx.conn, ctx.user, spec["bike_id"])

    new_type = ctx.body.get("spec_type", "__missing__")
    if new_type == "__missing__":
        raise HttpError(400, "spec_type is required (null clears the override)")
    if new_type is not None and new_type not in ("fixed", "pref", "community"):
        raise HttpError(400, "spec_type must be fixed, pref, community, or null")

    ctx.conn.execute("UPDATE specs SET spec_type=?, updated_at=datetime('now')"
                     " WHERE id=?", (new_type, spec_id))
    ctx.conn.commit()
    effective = new_type or spec["field_type"]
    return {"ok": True, "spec_type": effective,
            "overridden": new_type is not None,
            "field_default": spec["field_type"]}


@route("POST", r"/api/bikes/(\d+)/header", role="manager")
def pin_header_spec(ctx):
    """Lift one spec into this bike's hero.

    Per bike, not by moving the field into General. General is a Spec Tree
    property and lands on all 261 bikes at once; a manager is scoped to the
    bikes assigned to them, so reshaping one hero must not reshape the rest.
    Same reasoning as the per-bike Fixed tag.

    Pins sit on top of General rather than replacing it, so the hero never ends
    up emptier than the site-wide default.
    """
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)

    field_key = (ctx.body.get("field_key") or "").strip()
    if not field_key:
        raise HttpError(400, "field_key is required")

    # The spec has to exist ON THIS BIKE. Pinning by field_key alone would let
    # a hero advertise a field the bike does not carry.
    spec = one(ctx.conn.execute(
        "SELECT s.id, f.label, f.category FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE s.bike_id = ? AND s.field_key = ?", (bike_id, field_key)))
    if not spec:
        raise HttpError(404, "this bike has no such spec")

    if spec["category"] == "General":
        # A General field is in the header by Spec Tree rule, so the only thing
        # a manager can do to it here is take it OUT of this one bike's header.
        # Pinning it would be a no-op, and asking to pin it is almost always a
        # misread of the control, so say what is possible instead.
        if not ctx.body.get("hidden"):
            raise HttpError(409, "General specs are in every bike's header"
                                 " already — send hidden: true to take this"
                                 " one out of this bike's header")
        ctx.conn.execute(
            "INSERT INTO bike_header_specs"
            " (bike_id, field_key, sort_order, hidden, pinned_by)"
            " VALUES (?,?,0,1,?)"
            " ON CONFLICT(bike_id, field_key) DO UPDATE SET"
            "   hidden = 1, pinned_by = excluded.pinned_by",
            (bike_id, field_key, ctx.user["id"]))
        ctx.conn.commit()
        return {"ok": True, "field_key": field_key, "label": spec["label"],
                "in_header": False, "hidden": True}

    # Shown in the header INSTEAD of its section, rather than as well. The
    # quicklist carries no vote, flag or request controls, so this trades those
    # away — worth it for a headline figure nobody argues about, and the
    # manager is the one who knows which of their specs those are.
    hide_below = bool(ctx.body.get("hide_below"))

    nxt = ctx.conn.execute(
        "SELECT COALESCE(MAX(sort_order), 0) + 10 FROM bike_header_specs"
        " WHERE bike_id = ?", (bike_id,)).fetchone()[0]
    # Re-pinning an already-pinned spec is how the choice gets changed, so this
    # updates rather than ignores.
    ctx.conn.execute(
        "INSERT INTO bike_header_specs"
        " (bike_id, field_key, sort_order, hide_below, hidden, pinned_by)"
        " VALUES (?,?,?,?,0,?)"
        " ON CONFLICT(bike_id, field_key) DO UPDATE SET"
        "   hide_below = excluded.hide_below, hidden = 0,"
        "   pinned_by = excluded.pinned_by",
        (bike_id, field_key, nxt, 1 if hide_below else 0, ctx.user["id"]))
    ctx.conn.commit()
    return {"ok": True, "field_key": field_key, "label": spec["label"],
            "in_header": True, "hide_below": hide_below}


@route("DELETE", r"/api/bikes/(\d+)/header/([a-z0-9_]+)", role="manager")
def unpin_header_spec(ctx):
    """Take a pinned spec back out of this bike's hero.

    Only ever removes a pin. A General field cannot be unpinned here because it
    was never pinned — it is in the hero by Spec Tree decision, and undoing that
    from one bike's page is exactly the site-wide reach this stays clear of.
    """
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    field_key = ctx.params[1]

    cur = ctx.conn.execute(
        "DELETE FROM bike_header_specs WHERE bike_id=? AND field_key=?",
        (bike_id, field_key))
    ctx.conn.commit()
    if cur.rowcount == 0:
        raise HttpError(404, "that spec is not pinned to this bike's hero")
    return {"ok": True, "field_key": field_key, "in_header": False}


@route("POST", r"/api/specs/(\d+)/split-year", role="manager")
def split_spec_year(ctx):
    """Give one spec a different value from a given model year onward.

    The bike is not split. A tank that grew in 1978 is the same machine with a
    bigger tank -- same engine, same frame -- and splitting the bike would
    duplicate the twenty-eight specs that did not change to vary the two that
    did. Splitting the BIKE is for when the machine itself changed.

    The existing row keeps the earlier years; a new row carries `at_year`
    onward with the same value copied in, because the manager is about to
    correct it and starting from what it was beats starting from blank.
    """
    spec_id = int(ctx.params[0])
    spec = one(ctx.conn.execute(
        "SELECT s.*, f.label, b.year_start, b.year_end"
        " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
        " JOIN bikes b ON b.id = s.bike_id WHERE s.id=?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    require_manages(ctx.conn, ctx.user, spec["bike_id"])

    # An open-ended bike has no last year to split against.
    last = spec["year_to"] if spec["year_to"] is not None else spec["year_end"]
    first = spec["year_from"] if spec["year_from"] is not None else spec["year_start"]
    if last is None:
        raise HttpError(409, "this bike has no end year, so there is no range"
                             " to split — set its years first")

    try:
        at = int(ctx.field("at_year"))
    except (TypeError, ValueError):
        raise HttpError(400, "at_year must be a year")
    if not first < at <= last:
        raise HttpError(400, f"pick a year after {first} and no later than"
                             f" {last} — {at} leaves nothing on one side")

    ctx.conn.execute(
        "UPDATE specs SET year_from=?, year_to=?, updated_at=datetime('now')"
        " WHERE id=?", (first, at - 1, spec_id))
    # The copy carries the original's provenance, not the splitter's name.
    # Cutting a year span in two is not sourcing a value, and signing the new
    # row with whoever happened to do it would invent an attribution.
    cur = ctx.conn.execute(
        "INSERT INTO specs (bike_id, field_key, value, confidence, spec_type,"
        " tools, entered_by, value_source, year_from, year_to)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (spec["bike_id"], spec["field_key"], spec["value"], spec["confidence"],
         spec["spec_type"], spec["tools"], spec["entered_by"], spec["value_source"],
         at, last))
    ctx.conn.commit()
    return {"ok": True, "label": spec["label"],
            "earlier": {"id": spec_id, "year_from": first, "year_to": at - 1},
            "later": {"id": cur.lastrowid, "year_from": at, "year_to": last}}


@route("POST", r"/api/specs/(\d+)/merge-years", role="manager")
def merge_spec_years(ctx):
    """Undo a year split: this variant's value becomes the whole bike's again.

    Destructive in one direction -- the other variants' values are discarded --
    so the values being dropped come back in the response for the console to
    name before it asks.
    """
    spec_id = int(ctx.params[0])
    spec = one(ctx.conn.execute(
        "SELECT s.*, f.label FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key WHERE s.id=?",
        (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    require_manages(ctx.conn, ctx.user, spec["bike_id"])
    if spec["year_from"] is None:
        raise HttpError(409, "this spec already covers every year")

    others = rows(ctx.conn.execute(
        "SELECT id, value, year_from, year_to FROM specs"
        " WHERE bike_id=? AND field_key=? AND id<>?",
        (spec["bike_id"], spec["field_key"], spec_id)))
    if not others:
        # Nothing to merge with; just widen it back out.
        ctx.conn.execute(
            "UPDATE specs SET year_from=NULL, year_to=NULL,"
            " updated_at=datetime('now') WHERE id=?", (spec_id,))
        ctx.conn.commit()
        return {"ok": True, "label": spec["label"], "dropped": []}

    if not ctx.body.get("force"):
        raise HttpError(409, "merging discards the other year ranges for this"
                             " spec — confirm to go ahead")

    # Alternates, votes, flags and requests on the discarded rows go with them,
    # which is why this asks first.
    ctx.conn.executemany("DELETE FROM specs WHERE id=?",
                         [(o["id"],) for o in others])
    ctx.conn.execute(
        "UPDATE specs SET year_from=NULL, year_to=NULL,"
        " updated_at=datetime('now') WHERE id=?", (spec_id,))
    ctx.conn.commit()
    return {"ok": True, "label": spec["label"],
            "kept": spec["value"],
            "dropped": [{"value": o["value"], "year_from": o["year_from"],
                         "year_to": o["year_to"]} for o in others]}


@route("POST", r"/api/specs/(\d+)/pause", role="manager")
def toggle_spec_pause(ctx):
    """Take one spec offline, or put it back online.

    The same hide-without-destroying idea the tools and links already use,
    applied to the value itself. A spec goes offline when it might be wrong —
    a flag has come in and nobody has checked it yet — and leaving it up means
    riders keep reading a number as fact while it is in doubt. Deleting it
    would take the value, its alternates and their votes with it.

    Per bike and per spec, so pausing a torque figure on one machine says
    nothing about the same field on the other 260.
    """
    spec_id = int(ctx.params[0])
    spec = one(ctx.conn.execute(
        "SELECT s.id, s.bike_id, s.paused, f.label"
        " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE s.id = ?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    require_manages(ctx.conn, ctx.user, spec["bike_id"])

    # Explicit when given, toggle when not, so the button works either way and
    # two managers clicking at once cannot land on opposite states.
    want = ctx.body.get("paused")
    new = (0 if spec["paused"] else 1) if want is None else (1 if want else 0)

    # Resuming clears who paused it and when: those record a spec that is
    # currently offline, not a history of every time one ever was.
    ctx.conn.execute(
        "UPDATE specs SET paused=?, paused_by=?,"
        "  paused_at = CASE WHEN ?=1 THEN datetime('now') ELSE NULL END,"
        "  updated_at = datetime('now')"
        " WHERE id=?",
        (new, ctx.user["id"] if new else None, new, spec_id))
    ctx.conn.commit()
    return {"ok": True, "spec_id": spec_id, "paused": bool(new),
            "label": spec["label"]}


@route("POST", r"/api/specs/(\d+)/vote", role="user")
def toggle_spec_vote(ctx):
    """Upvote the stock value — 'this is right, it fitted mine'."""
    spec_id = int(ctx.params[0])
    refuse_if_paused(ctx.conn, ctx.user, spec_id)
    spec = one(ctx.conn.execute("SELECT value FROM specs WHERE id=?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    if spec["value"] in (None, ""):
        raise HttpError(400, "there is no value here to vote on yet — "
                             "request it, or add one")
    if ctx.conn.execute("SELECT 1 FROM spec_votes WHERE spec_id=? AND user_id=?",
                        (spec_id, ctx.user["id"])).fetchone():
        ctx.conn.execute("DELETE FROM spec_votes WHERE spec_id=? AND user_id=?",
                         (spec_id, ctx.user["id"]))
        voted = False
    else:
        ctx.conn.execute("INSERT INTO spec_votes (spec_id, user_id) VALUES (?,?)",
                         (spec_id, ctx.user["id"]))
        voted = True
    ctx.conn.commit()
    votes = ctx.conn.execute(
        "SELECT votes FROM spec_vote_counts WHERE spec_id=?", (spec_id,)).fetchone()[0]
    return {"voted": voted, "votes": votes}


@route("POST", r"/api/specs/(\d+)/request", role="user")
def toggle_spec_request(ctx):
    """"I want this one filled in." Only meaningful on a gap — once a value
    exists the request is answered, so asking again would just be noise."""
    spec_id = int(ctx.params[0])
    refuse_if_paused(ctx.conn, ctx.user, spec_id)
    spec = one(ctx.conn.execute("SELECT value FROM specs WHERE id=?", (spec_id,)))
    if not spec:
        raise HttpError(404, "spec not found")
    if spec["value"] not in (None, ""):
        raise HttpError(400, "this spec already has a value — "
                             "flag it if you think it is wrong")
    if ctx.conn.execute("SELECT 1 FROM spec_requests WHERE spec_id=? AND user_id=?",
                        (spec_id, ctx.user["id"])).fetchone():
        ctx.conn.execute("DELETE FROM spec_requests WHERE spec_id=? AND user_id=?",
                         (spec_id, ctx.user["id"]))
        requested = False
    else:
        ctx.conn.execute("INSERT INTO spec_requests (spec_id, user_id) VALUES (?,?)",
                         (spec_id, ctx.user["id"]))
        requested = True
    ctx.conn.commit()
    count = ctx.conn.execute(
        "SELECT requests FROM spec_request_counts WHERE spec_id=?", (spec_id,)).fetchone()[0]
    return {"requested": requested, "requests": count}


@route("POST", r"/api/alternates/(\d+)/flag", role="user")
def flag_alternate(ctx):
    """Flag one alternate rather than the spec. Recorded on the same table as a
    stock-value flag, distinguished by alternate_id, so the manager's queue is
    still one query and one place to work."""
    alt_id = int(ctx.params[0])
    alt = one(ctx.conn.execute(
        "SELECT spec_id FROM spec_alternates WHERE id=?", (alt_id,)))
    if not alt:
        raise HttpError(404, "alternate not found")
    reason = ctx.field("reason")
    if reason not in ("irrelevant", "incorrect", "inappropriate", "other"):
        raise HttpError(400, "unknown reason")
    detail = ctx.field("detail", required=False)
    if detail and len(detail) > 150:
        raise HttpError(400, "detail is limited to 150 characters")
    if ctx.conn.execute(
            "SELECT 1 FROM value_flags WHERE alternate_id=? AND flagged_by=?"
            " AND status='open'", (alt_id, ctx.user["id"])).fetchone():
        raise HttpError(409, "you have already flagged this alternate")
    cur = ctx.conn.execute(
        "INSERT INTO value_flags (spec_id, alternate_id, flagged_by, reason, detail)"
        " VALUES (?,?,?,?,?)",
        (alt["spec_id"], alt_id, ctx.user["id"], reason, detail))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


@route("POST", r"/api/alternates/(\d+)/pause", role="manager")
def toggle_alternate_pause(ctx):
    """Take somebody's alternative down, or put it back: the manager's
    answer to "that one is wrong for this bike".

    Hidden, not deleted -- the text, who suggested it and its votes stay,
    so the decision can be undone and the person is not erased. Until now
    this could only be reached by resolving a flag, which meant a manager
    had to flag an alternate themselves before they could act on it.
    """
    alt_id = int(ctx.params[0])
    a = one(ctx.conn.execute(
        "SELECT a.id, a.text, a.paused, s.bike_id, f.label"
        " FROM spec_alternates a JOIN specs s ON s.id = a.spec_id"
        " JOIN spec_fields f ON f.field_key = s.field_key WHERE a.id=?", (alt_id,)))
    if not a:
        raise HttpError(404, "no such alternative")
    require_manages(ctx.conn, ctx.user, a["bike_id"])
    want = ctx.body.get("paused")
    new = (0 if a["paused"] else 1) if want is None else (1 if want else 0)
    ctx.conn.execute("UPDATE spec_alternates SET paused=? WHERE id=?", (new, alt_id))
    if ctx.user["role"] == "admin":
        log_action(ctx, "alternate.pause" if new else "alternate.unpause",
                   f'{"Hid" if new else "Restored"} the alternative "{a["text"]}" on {a["label"]}',
                   target=str(alt_id), detail={"bike_id": a["bike_id"]})
    ctx.conn.commit()
    return {"ok": True, "paused": bool(new), "text": a["text"]}


@route("POST", r"/api/alternates/(\d+)/vote", role="user")
def toggle_alternate_vote(ctx):
    alt_id = int(ctx.params[0])
    if not ctx.conn.execute("SELECT 1 FROM spec_alternates WHERE id=?", (alt_id,)).fetchone():
        raise HttpError(404, "alternate not found")
    existing = ctx.conn.execute(
        "SELECT 1 FROM alternate_votes WHERE alternate_id=? AND user_id=?",
        (alt_id, ctx.user["id"])).fetchone()
    if existing:
        ctx.conn.execute(
            "DELETE FROM alternate_votes WHERE alternate_id=? AND user_id=?",
            (alt_id, ctx.user["id"]))
        voted = False
    else:
        ctx.conn.execute(
            "INSERT INTO alternate_votes (alternate_id, user_id) VALUES (?,?)",
            (alt_id, ctx.user["id"]))
        voted = True
    ctx.conn.commit()
    votes = ctx.conn.execute(
        "SELECT votes FROM alternate_vote_counts WHERE alternate_id=?",
        (alt_id,)).fetchone()[0]
    return {"voted": voted, "votes": votes}


@route("POST", r"/api/specs/(\d+)/flags", role="user")
def flag_spec(ctx):
    spec_id = int(ctx.params[0])
    refuse_if_paused(ctx.conn, ctx.user, spec_id)
    if not ctx.conn.execute("SELECT 1 FROM specs WHERE id=?", (spec_id,)).fetchone():
        raise HttpError(404, "spec not found")
    reason = ctx.field("reason")
    if reason not in ("irrelevant", "incorrect", "inappropriate", "other"):
        raise HttpError(400, "unknown reason")
    detail = ctx.field("detail", required=False)
    if detail and len(detail) > 150:
        raise HttpError(400, "detail is limited to 150 characters")
    # One open flag per person per value, matching the alternate endpoint.
    # Without this the same rider could raise the same complaint repeatedly and
    # the count would read as several people disagreeing.
    if ctx.conn.execute(
            "SELECT 1 FROM value_flags WHERE spec_id=? AND alternate_id IS NULL"
            " AND flagged_by=? AND status='open'",
            (spec_id, ctx.user["id"])).fetchone():
        raise HttpError(409, "you have already flagged this value")
    cur = ctx.conn.execute(
        "INSERT INTO value_flags (spec_id, flagged_by, reason, detail)"
        " VALUES (?,?,?,?)", (spec_id, ctx.user["id"], reason, detail))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


@route("POST", r"/api/bikes/(\d+)/specs/([a-z0-9_]+)", role="manager")
def add_spec_to_bike(ctx):
    """Put a field from the tree onto one bike -- the manager's side of admin's
    "add to bikes", scoped to the bikes they manage.

    The row arrives empty and pending, the same as from the questionnaire or
    admin's apply: a field is a question, not an answer. The offline-default
    trigger applies as everywhere else, so a sprocket added to a scooter
    starts hidden and the reply says so. Riders who had asked for the field
    on this bike are resolved as "added" -- it is there now for all of them.
    """
    bike_id, key = int(ctx.params[0]), ctx.params[1]
    require_manages(ctx.conn, ctx.user, bike_id)
    field = one(ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field")
    cur = ctx.conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " VALUES (?,?,NULL,'pending')", (bike_id, key))
    if not cur.rowcount:
        raise HttpError(409, f"this bike already has {field['label']}")
    spec = one(ctx.conn.execute("SELECT id, paused FROM specs WHERE id=?", (cur.lastrowid,)))
    resolved = ctx.conn.execute(
        "UPDATE field_requests SET status='added', decided_by=?, resolved_at=datetime('now')"
        " WHERE bike_id=? AND field_key=? AND status='pending'",
        (ctx.user["id"], bike_id, key)).rowcount
    if ctx.user["role"] == "admin":
        log_action(ctx, "field.apply", f'Added "{field["label"]}" to bike #{bike_id}',
                   target=key, detail={"bike_ids": [bike_id], "added": 1, "resolved": resolved})
    ctx.conn.commit()
    return {"ok": True, "spec_id": spec["id"], "paused": bool(spec["paused"]),
            "label": field["label"], "resolved": resolved}


@route("DELETE", r"/api/bikes/(\d+)/specs/([a-z0-9_]+)", role="manager")
def remove_spec_from_bike(ctx):
    """Take a field off one bike: the manager's answer to "this doesn't belong
    here" -- a coolant capacity on an air-cooled engine, a drive belt on a
    chain-drive bike.

    Only while the row is empty. A value or an alternate is somebody's work;
    destroying it is admin's call, through the tree's "remove from a bike"
    with its confirmation. Open flags on the row go with it -- the removal is
    what they asked for -- and the bike's manager is not blocked by the
    field being universal: universal means every bike gets it, not that no
    bike can lose it.
    """
    bike_id, key = int(ctx.params[0]), ctx.params[1]
    require_manages(ctx.conn, ctx.user, bike_id)
    spec = one(ctx.conn.execute(
        "SELECT s.id, s.value, f.label,"
        "  (SELECT COUNT(*) FROM spec_alternates a WHERE a.spec_id = s.id) AS alternates,"
        "  (SELECT COUNT(*) FROM value_flags vf WHERE vf.spec_id = s.id AND vf.status='open') AS flags"
        " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE s.bike_id=? AND s.field_key=?", (bike_id, key)))
    if not spec:
        raise HttpError(404, "this bike does not have that field")
    if spec["value"] not in (None, "") or spec["alternates"]:
        raise HttpError(409, f"{spec['label']} holds a value or an alternate on this bike -- "
                             "clear it first, or ask admin to remove it")
    ctx.conn.execute("DELETE FROM specs WHERE id=?", (spec["id"],))
    forget_placements(ctx.conn, bike_id, key)
    if ctx.user["role"] == "admin":
        log_action(ctx, "spec.remove", f'Removed "{spec["label"]}" from bike #{bike_id}',
                   target=key, detail={"bike_id": bike_id, "flags_closed": spec["flags"]})
    ctx.conn.commit()
    return {"ok": True, "flags_closed": spec["flags"]}


@route("DELETE", r"/api/flags/(\d+)", role="user")
def withdraw_flag(ctx):
    """'Flagged by mistake' — only your own, and only while still open."""
    flag_id = int(ctx.params[0])
    f = one(ctx.conn.execute("SELECT * FROM value_flags WHERE id=?", (flag_id,)))
    if not f:
        raise HttpError(404, "flag not found")
    if f["flagged_by"] != ctx.user["id"]:
        raise HttpError(403, "not your flag")
    if f["status"] != "open":
        raise HttpError(400, "this flag has already been resolved")
    ctx.conn.execute("DELETE FROM value_flags WHERE id=?", (flag_id,))
    ctx.conn.commit()
    return {"ok": True}


# ===========================================================================
# MANAGER DASHBOARD
# ===========================================================================
def _managed_bike_ids(conn, user):
    if user["role"] == "admin":
        return [r["id"] for r in rows(conn.execute("SELECT id FROM bikes"))]
    return [r["bike_id"] for r in rows(conn.execute(
        "SELECT bike_id FROM bike_managers WHERE user_id=?", (user["id"],)))]


@route("GET", r"/api/manager/bikes", role="manager")
def manager_bikes(ctx):
    return {"bikes": rows(ctx.conn.execute(
        "SELECT d.*, p.fields_triggered, p.specs_filled, p.specs_needed,"
        " m.specialty,"
        " (SELECT COUNT(*) FROM value_flags vf JOIN specs s ON s.id=vf.spec_id"
        "   WHERE s.bike_id=d.bike_id AND vf.status='open') AS open_flags"
        " FROM bike_managers m"
        " JOIN bike_display d ON d.bike_id = m.bike_id"
        " JOIN bike_spec_progress p ON p.bike_id = m.bike_id"
        " WHERE m.user_id = ? ORDER BY d.display_name", (ctx.user["id"],)))}


@route("GET", r"/api/manager/summary", role="manager")
def manager_summary(ctx):
    ids = _managed_bike_ids(ctx.conn, ctx.user)
    if not ids:
        return {"bikes_managed": 0, "specs_filled": 0, "specs_needed": 0,
                "open_flags": 0, "not_sure_pending": 0, "proposals": 0}
    ph = ",".join("?" * len(ids))
    agg = one(ctx.conn.execute(
        f"SELECT COALESCE(SUM(specs_filled),0) AS specs_filled,"
        f" COALESCE(SUM(specs_needed),0) AS specs_needed"
        f" FROM bike_spec_progress WHERE bike_id IN ({ph})", ids))
    open_flags = ctx.conn.execute(
        f"SELECT COUNT(*) FROM value_flags vf JOIN specs s ON s.id=vf.spec_id"
        f" WHERE vf.status='open' AND s.bike_id IN ({ph})", ids).fetchone()[0]
    not_sure = ctx.conn.execute(
        "SELECT COUNT(*) FROM not_sure_answers WHERE submitted_by=? AND status='pending'",
        (ctx.user["id"],)).fetchone()[0]
    proposals = ctx.conn.execute(
        "SELECT COUNT(*) FROM branch_proposals WHERE proposed_by=?",
        (ctx.user["id"],)).fetchone()[0]
    submissions = ctx.conn.execute(
        f"SELECT COUNT(*) FROM specs s WHERE s.bike_id IN ({ph})"
        f" AND s.value IS NOT NULL AND s.value <> '' AND s.confidence='pending'"
        f" AND s.entered_by IS NOT NULL"
        f" AND NOT EXISTS (SELECT 1 FROM bike_managers m"
        f"                 WHERE m.bike_id = s.bike_id AND m.user_id = s.entered_by)",
        ids).fetchone()[0]
    requested = ctx.conn.execute(
        f"SELECT COUNT(*) FROM specs s"
        f" JOIN spec_request_counts rc ON rc.spec_id = s.id"
        f" WHERE s.bike_id IN ({ph}) AND (s.value IS NULL OR s.value='')"
        f"   AND rc.requests > 0", ids).fetchone()[0]
    field_requests = ctx.conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 FROM field_requests WHERE bike_id IN ({ph})"
        f" AND status='pending' GROUP BY bike_id, field_key)", ids).fetchone()[0]
    return {"bikes_managed": len(ids), **agg, "open_flags": open_flags,
            "not_sure_pending": not_sure, "proposals": proposals,
            "submissions": submissions, "requested_gaps": requested,
            "field_requests": field_requests}


@route("GET", r"/api/manager/flags", role="manager")
def manager_flags(ctx):
    ids = _managed_bike_ids(ctx.conn, ctx.user)
    if not ids:
        return {"flags": []}
    ph = ",".join("?" * len(ids))
    return {"flags": rows(ctx.conn.execute(
        f"SELECT vf.id, vf.reason, vf.detail, vf.created_at, vf.spec_id,"
        # A flag may target the stock value or one alternate. alt_text tells
        # them apart, so a manager is never asked to "fix" a value that is
        # actually somebody's competing suggestion.
        f"       vf.alternate_id, alt.text AS alt_text,"
        f"       f.label AS spec_label, f.example, s.value AS current_value, s.bike_id, s.field_key,"
        # So the queue can offer the right actions: whether the spec is already
        # offline, and what kind of value it is, since a wire colour cannot be
        # fixed by typing into a text box.
        f"       s.paused AS spec_offline, f.value_type,"
        f"       d.display_name AS bike_name,"
        f"       fb.id AS flagged_by_id, fb.username AS flagged_by,"
        f"       eb.id AS entered_by_id,"
        f"       COALESCE(eb.username,'—') AS entered_by"
        f" FROM value_flags vf"
        f" JOIN specs s ON s.id = vf.spec_id"
        f" JOIN spec_fields f ON f.field_key = s.field_key"
        f" JOIN bike_display d ON d.bike_id = s.bike_id"
        f" JOIN users fb ON fb.id = vf.flagged_by"
        f" LEFT JOIN users eb ON eb.id = s.entered_by"
        f" LEFT JOIN spec_alternates alt ON alt.id = vf.alternate_id"
        f" WHERE vf.status='open' AND s.bike_id IN ({ph})"
        f" ORDER BY vf.created_at DESC", ids))}


@route("GET", r"/api/manager/submissions", role="manager")
def manager_submissions(ctx):
    """Values the public filled into gaps on your bikes.

    Derived, not a separate queue table: a submission is a spec that has a
    value, is still 'pending', and was entered by somebody who does not manage
    the bike. Confirming it raises the confidence, which is what removes it
    from this list — so the queue cannot drift out of step with the data.
    """
    ids = _managed_bike_ids(ctx.conn, ctx.user)
    if not ids:
        return {"submissions": []}
    ph = ",".join("?" * len(ids))
    return {"submissions": rows(ctx.conn.execute(
        f"SELECT s.id AS spec_id, s.value, s.updated_at, s.bike_id,"
        f"       f.label AS spec_label, f.category, f.spec_type,"
        f"       u.id AS entered_by_id, u.username AS entered_by,"
        f"       d.display_name AS bike_name"
        f" FROM specs s"
        f" JOIN spec_fields f ON f.field_key = s.field_key"
        f" JOIN users u ON u.id = s.entered_by"
        f" JOIN bike_display d ON d.bike_id = s.bike_id"
        f" WHERE s.bike_id IN ({ph})"
        f"   AND s.value IS NOT NULL AND s.value <> ''"
        f"   AND s.confidence = 'pending'"
        f"   AND NOT EXISTS (SELECT 1 FROM bike_managers m"
        f"                   WHERE m.bike_id = s.bike_id AND m.user_id = s.entered_by)"
        f" ORDER BY s.updated_at DESC", ids))}


@route("GET", r"/api/manager/requests", role="manager")
def manager_requests(ctx):
    """Gaps on your bikes, ranked by how many riders asked for them.

    This is the whole point of the request button: 40 empty fields with no
    ordering is a wall, the same 40 with "6 riders asked for this" at the top is
    a to-do list.
    """
    ids = _managed_bike_ids(ctx.conn, ctx.user)
    if not ids:
        return {"requests": []}
    ph = ",".join("?" * len(ids))
    return {"requests": rows(ctx.conn.execute(
        f"SELECT s.id AS spec_id, s.field_key, f.label AS spec_label, f.category,"
        f"       rc.requests, d.display_name AS bike_name, s.bike_id,"
        f"       (SELECT MAX(created_at) FROM spec_requests r WHERE r.spec_id=s.id)"
        f"         AS last_requested"
        f" FROM specs s"
        f" JOIN spec_fields f ON f.field_key = s.field_key"
        f" JOIN spec_request_counts rc ON rc.spec_id = s.id"
        f" JOIN bike_display d ON d.bike_id = s.bike_id"
        f" WHERE s.bike_id IN ({ph})"
        f"   AND (s.value IS NULL OR s.value = '')"
        f"   AND rc.requests > 0"
        f" ORDER BY rc.requests DESC, f.label", ids))}


@route("GET", r"/api/manager/resolved", role="manager")
def manager_resolved(ctx):
    ids = _managed_bike_ids(ctx.conn, ctx.user)
    if not ids:
        return {"resolved": []}
    ph = ",".join("?" * len(ids))
    return {"resolved": rows(ctx.conn.execute(
        f"SELECT vf.id, vf.status AS outcome, vf.old_value, vf.new_value,"
        f"       vf.resolved_at, f.label AS spec_label"
        f" FROM value_flags vf"
        f" JOIN specs s ON s.id = vf.spec_id"
        f" JOIN spec_fields f ON f.field_key = s.field_key"
        f" WHERE vf.status IN ('fixed','dismissed') AND s.bike_id IN ({ph})"
        f" ORDER BY vf.resolved_at DESC LIMIT 50", ids))}


@route("POST", r"/api/flags/(\d+)/fix", role="manager")
def fix_flag(ctx):
    """Set the value directly — no vote. The assigned manager is the expert for
    this bike, which is the entire reason the flag was routed here."""
    flag_id = int(ctx.params[0])
    f = one(ctx.conn.execute(
        "SELECT vf.*, s.bike_id, s.value AS current_value"
        " FROM value_flags vf JOIN specs s ON s.id = vf.spec_id"
        " WHERE vf.id=?", (flag_id,)))
    if not f:
        raise HttpError(404, "flag not found")
    if f["status"] != "open":
        raise HttpError(400, "this flag is already resolved")
    if f["alternate_id"] is not None:
        # This flag is against somebody's alternate, not the stock value.
        # Writing new_value here would overwrite the manual's value with an
        # edit that was never aimed at it.
        raise HttpError(400, "this flag is against an alternate, not the stock "
                             "value — dismiss it, or edit the spec directly")
    require_manages(ctx.conn, ctx.user, f["bike_id"])

    new_value = check_value(ctx.conn, f["spec_id"], ctx.field("new_value"))
    was_by = ctx.conn.execute("SELECT entered_by FROM specs WHERE id=?", (f["spec_id"],)).fetchone()[0]
    ctx.conn.execute(
        "UPDATE specs SET value=?, value_source=NULL, entered_by=?, updated_at=datetime('now')"
        " WHERE id=?", (new_value, ctx.user["id"], f["spec_id"]))
    ctx.conn.execute(
        "UPDATE value_flags SET status='fixed', old_value=?, new_value=?, old_entered_by=?,"
        " resolved_by=?, resolved_at=datetime('now') WHERE id=?",
        (f["current_value"], new_value, was_by, ctx.user["id"], flag_id))
    ctx.conn.commit()
    return {"ok": True}


@route("POST", r"/api/flags/(\d+)/remove-alternate", role="manager")
def remove_flagged_alternate(ctx):
    """Take down the alternate a flag is about, and close the flag.

    Fixing the stock value is the wrong answer to a flag against somebody's
    competing suggestion — the stock value was never what was complained about.
    Until now that left the manager with nothing to do but dismiss, which
    records "the flag was not valid" about a flag that was.

    Hidden rather than deleted, the way a paused tool or link is: the text and
    its votes survive if the call turns out to be wrong.
    """
    flag_id = int(ctx.params[0])
    f = one(ctx.conn.execute(
        "SELECT vf.*, s.bike_id FROM value_flags vf"
        " JOIN specs s ON s.id = vf.spec_id WHERE vf.id=?", (flag_id,)))
    if not f:
        raise HttpError(404, "flag not found")
    if f["status"] != "open":
        raise HttpError(400, "this flag is already resolved")
    if f["alternate_id"] is None:
        raise HttpError(400, "this flag is against the stock value, not an "
                             "alternate — fix the value instead")
    require_manages(ctx.conn, ctx.user, f["bike_id"])

    alt = one(ctx.conn.execute("SELECT text FROM spec_alternates WHERE id=?",
                               (f["alternate_id"],)))
    ctx.conn.execute("UPDATE spec_alternates SET paused=1 WHERE id=?",
                     (f["alternate_id"],))
    ctx.conn.execute(
        "UPDATE value_flags SET status='fixed', old_value=?,"
        " new_value='(alternate hidden)', resolved_by=?,"
        " resolved_at=datetime('now') WHERE id=?",
        (alt["text"] if alt else None, ctx.user["id"], flag_id))
    ctx.conn.commit()
    return {"ok": True, "hidden": alt["text"] if alt else None}


@route("POST", r"/api/flags/(\d+)/offline", role="manager")
def take_flagged_spec_offline(ctx):
    """Take the spec offline from the flag queue, where the triage happens.

    A flag saying a value is wrong is exactly when it should stop being read as
    fact, and the manager is looking at the queue rather than the bike page. The
    flag stays OPEN: going offline buys time to check, it does not decide
    anything, and closing it here would lose the thing still to be done.
    """
    flag_id = int(ctx.params[0])
    f = one(ctx.conn.execute(
        "SELECT vf.*, s.bike_id, f.label FROM value_flags vf"
        " JOIN specs s ON s.id = vf.spec_id"
        " JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE vf.id=?", (flag_id,)))
    if not f:
        raise HttpError(404, "flag not found")
    require_manages(ctx.conn, ctx.user, f["bike_id"])

    want = ctx.body.get("offline", True)
    paused = 1 if want else 0
    ctx.conn.execute(
        "UPDATE specs SET paused=?, paused_by=?,"
        "  paused_at = CASE WHEN ?=1 THEN datetime('now') ELSE NULL END,"
        "  updated_at = datetime('now') WHERE id=?",
        (paused, ctx.user["id"] if paused else None, paused, f["spec_id"]))
    ctx.conn.commit()
    return {"ok": True, "spec_id": f["spec_id"], "label": f["label"],
            "offline": bool(paused)}


@route("POST", r"/api/flags/(\d+)/dismiss", role="manager")
def dismiss_flag(ctx):
    """Tracked separately from a fix, so the flagger's history reflects which
    of their flags led to a real change and which did not."""
    flag_id = int(ctx.params[0])
    f = one(ctx.conn.execute(
        "SELECT vf.*, s.bike_id, s.value AS current_value"
        " FROM value_flags vf JOIN specs s ON s.id = vf.spec_id"
        " WHERE vf.id=?", (flag_id,)))
    if not f:
        raise HttpError(404, "flag not found")
    if f["status"] != "open":
        raise HttpError(400, "this flag is already resolved")
    require_manages(ctx.conn, ctx.user, f["bike_id"])
    ctx.conn.execute(
        "UPDATE value_flags SET status='dismissed', old_value=?, resolved_by=?,"
        " resolved_at=datetime('now') WHERE id=?",
        (f["current_value"], ctx.user["id"], flag_id))
    ctx.conn.commit()
    return {"ok": True}


@route("POST", r"/api/flags/(\d+)/messages", role="manager")
def message_flagger(ctx):
    flag_id = int(ctx.params[0])
    f = one(ctx.conn.execute(
        "SELECT vf.*, s.bike_id FROM value_flags vf"
        " JOIN specs s ON s.id=vf.spec_id WHERE vf.id=?", (flag_id,)))
    if not f:
        raise HttpError(404, "flag not found")
    require_manages(ctx.conn, ctx.user, f["bike_id"])

    # Who the manager needs is not always the flagger. "Was this on a later
    # production run?" goes to the rider who raised it; "this field is wrong
    # for the whole platform" goes to an admin, and having to leave the queue
    # to find one is how that conversation does not happen.
    to = ctx.body.get("to", "flagger")
    if to == "flagger":
        recipient = f["flagged_by"]
    elif to == "admin":
        row = one(ctx.conn.execute(
            "SELECT id FROM users WHERE role='admin' AND suspended=0"
            " ORDER BY id LIMIT 1"))
        if not row:
            raise HttpError(409, "there is no admin to write to")
        recipient = row["id"]
    else:
        raise HttpError(400, "to must be 'flagger' or 'admin'")

    if recipient == ctx.user["id"]:
        raise HttpError(400, "that is you — pick the other recipient")

    cur = ctx.conn.execute(
        "INSERT INTO flag_messages (value_flag_id, from_user, to_user, body)"
        " VALUES (?,?,?,?)",
        (flag_id, ctx.user["id"], recipient, ctx.field("body")))
    ctx.conn.commit()
    return {"id": cur.lastrowid, "to": to}


@route("GET", r"/api/manager/not-sure", role="manager")
def manager_not_sure(ctx):
    return {"items": rows(ctx.conn.execute(
        "SELECT n.id, n.question_text, n.status, n.created_at, n.admin_note,"
        " d.display_name AS bike_name"
        " FROM not_sure_answers n JOIN bike_display d ON d.bike_id=n.bike_id"
        " WHERE n.submitted_by=? ORDER BY n.created_at DESC",
        (ctx.user["id"],)))}


@route("GET", r"/api/manager/proposals", role="manager")
def manager_proposals(ctx):
    return {"proposals": rows(ctx.conn.execute(
        "SELECT id, field_name, category, status, reasoning, admin_note, created_at"
        " FROM branch_proposals WHERE proposed_by=? ORDER BY created_at DESC",
        (ctx.user["id"],)))}


@route("POST", r"/api/proposals", role="manager")
def create_proposal(ctx):
    cur = ctx.conn.execute(
        "INSERT INTO branch_proposals (field_name, category, bike_id,"
        " proposed_by, reasoning, value_type) VALUES (?,?,?,?,?,?)",
        (ctx.field("field_name"), ctx.field("category"),
         ctx.body.get("bike_id"), ctx.user["id"], ctx.field("reasoning"),
         value_type_from(ctx.body)))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


# ===========================================================================
# FIELD REQUESTS: "this bike should list X"
#
# A rider can ask for a spec the bike does not have. Two cases, one page:
#   * the field exists on the Spec Tree but not on this bike -> field_requests,
#     resolved by the bike's manager (add it or decline);
#   * the field is not on the tree at all -> a branch proposal, resolved by
#     admin, who owns what fields exist. Same table the managers' proposals
#     use, so the admin sees one queue.
# ===========================================================================
@route("GET", r"/api/bikes/(\d+)/field-requests")
def bike_field_requests(ctx):
    """Every field on the tree and where it stands ON THIS BIKE: online here,
    offline here (the row exists but is staged, so no rider sees it), or not
    on this bike. The last two can be asked for; an online one is already on
    the sheet, where "request this spec" applies. Public, so the page can show
    "3 riders asked for Coolant Capacity" before sign-in; `mine` is only ever
    true signed in.
    """
    bike_id = int(ctx.params[0])
    if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike_id,)).fetchone():
        raise HttpError(404, "bike not found")
    uid = ctx.user["id"] if ctx.user else -1
    order = {c: i for i, c in enumerate(CATEGORY_ORDER)}
    fields = rows(ctx.conn.execute(
        "SELECT f.field_key, f.label, f.category, f.value_type,"
        "  (SELECT s.paused FROM specs s WHERE s.bike_id=? AND s.field_key=f.field_key) AS here,"
        "  (SELECT COUNT(*) FROM field_requests r WHERE r.bike_id=? AND r.field_key=f.field_key"
        "     AND r.status='pending') AS requests,"
        "  EXISTS (SELECT 1 FROM field_requests r WHERE r.bike_id=? AND r.field_key=f.field_key"
        "     AND r.user_id=? AND r.status='pending') AS mine"
        " FROM spec_fields f"
        " ORDER BY f.category, f.sort_order, f.label", (bike_id, bike_id, bike_id, uid)))
    for f in fields:
        here = f.pop("here")
        f["status"] = "missing" if here is None else ("offline" if here else "online")
    fields.sort(key=lambda f: (order.get(f["category"], 99), f["label"].lower()))
    proposals = rows(ctx.conn.execute(
        "SELECT p.id, p.field_name, p.category, p.status, p.created_at, p.value_type,"
        "       p.proposed_by = ? AS mine"
        " FROM branch_proposals p WHERE p.bike_id=? AND p.status='pending'"
        " ORDER BY p.created_at DESC", (uid, bike_id)))
    return {"fields": fields, "proposals": proposals, "categories": CATEGORY_ORDER}


FIELD_REQUEST_MAX = 25


def _ask_for_field(ctx, bike_id, key, reasoning):
    """One rider asking for one field on one bike. Returns what was filed and
    how many riders are now waiting on it, or raises the reason it cannot be
    asked for -- which the batch below catches per spec."""
    field = one(ctx.conn.execute(
        "SELECT field_key, label FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field on the Spec Tree")
    if ctx.conn.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key=? AND paused=0",
                        (bike_id, key)).fetchone():
        raise HttpError(409, f"this bike already lists {field['label']} -- "
                             "use \"request this spec\" on it instead")
    try:
        cur = ctx.conn.execute(
            "INSERT INTO field_requests (bike_id, field_key, user_id, reasoning)"
            " VALUES (?,?,?,?)", (bike_id, key, ctx.user["id"], reasoning))
    except sqlite3.IntegrityError:
        raise HttpError(409, f"you have already asked for {field['label']} on this bike")
    n = ctx.conn.execute(
        "SELECT COUNT(*) FROM field_requests WHERE bike_id=? AND field_key=? AND status='pending'",
        (bike_id, key)).fetchone()[0]
    return {"id": cur.lastrowid, "field_key": key, "label": field["label"], "requests": n}


def _ask_for_fields(ctx, bike_id, keys, reasoning, one_only=False):
    """Several specs in one ask, sharing the one reason the rider typed.

    Each key is taken on its own: one already asked for is no reason to throw
    away the other four, so it comes back under `skipped` saying why and the
    rest go through. A single `field_key` keeps the old answer exactly -- one
    spec, one error if it cannot be asked for.
    """
    if not isinstance(keys, list):
        raise HttpError(400, "field_keys must be a list of field keys")
    wanted = []
    for k in keys:
        k = k.strip() if isinstance(k, str) else ""
        if k and k not in wanted:
            wanted.append(k)
    if not wanted:
        raise HttpError(400, "pick at least one spec from the list, or name the one that is missing")
    if len(wanted) > FIELD_REQUEST_MAX:
        raise HttpError(400, f"{len(wanted)} specs in one ask -- {FIELD_REQUEST_MAX} at a time is "
                             "the limit, so the rider who maintains this bike gets a list they "
                             "can work through")
    if one_only:
        got = _ask_for_field(ctx, bike_id, wanted[0], reasoning)
        ctx.conn.commit()
        return {"kind": "request", "id": got["id"], "requests": got["requests"]}

    asked, skipped = [], []
    for k in wanted:
        try:
            asked.append(_ask_for_field(ctx, bike_id, k, reasoning))
        except HttpError as e:
            skipped.append({"field_key": k, "why": e.message})
    if not asked:
        ctx.conn.rollback()
        raise HttpError(409, skipped[0]["why"] if len(skipped) == 1 else
                        "none of those could be asked for: "
                        + "; ".join(s["why"] for s in skipped))
    ctx.conn.commit()
    return {"kind": "request", "count": len(asked), "asked": asked, "skipped": skipped,
            "requests": asked[0]["requests"] if len(asked) == 1 else None}


@route("POST", r"/api/bikes/(\d+)/field-requests", role="user")
def request_field(ctx):
    """Ask for one or more fields on this bike. `field_key` for a single one
    on the tree, `field_keys` for several at once; or `field_name` +
    `category` for one that is not on the tree, which files a branch proposal
    for admin under this rider's name."""
    bike_id = int(ctx.params[0])
    if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike_id,)).fetchone():
        raise HttpError(404, "bike not found")
    reasoning = (ctx.body.get("reasoning") or "").strip() or None
    if reasoning and len(reasoning) > 500:
        raise HttpError(400, "reasoning is limited to 500 characters")

    # One spec, or a handful. A rider reading down the tree finds three or
    # four things this bike should list and does not, and one trip should
    # carry them all: `field_keys` for the list, `field_key` for a single.
    keys = ctx.body.get("field_keys")
    one_only = keys is None
    if one_only:
        single = (ctx.body.get("field_key") or "").strip()
        keys = [single] if single else []
    if keys or not one_only:
        return _ask_for_fields(ctx, bike_id, keys, reasoning, one_only)

    name = (ctx.body.get("field_name") or "").strip()
    category = ctx.body.get("category")
    if not name:
        raise HttpError(400, "pick a field from the list, or name the one that is missing")
    if len(name) > 120:
        raise HttpError(400, "field name is limited to 120 characters")
    if category not in CATEGORY_ORDER:
        raise HttpError(400, "category must be one of: " + ", ".join(CATEGORY_ORDER))
    would_be = re.sub(r"[^a-z0-9]+", "_",
                      name.lower().replace("×", " x ").replace("&", " and ")).strip("_")
    existing = one(ctx.conn.execute(
        "SELECT field_key, label FROM spec_fields WHERE field_key=? OR lower(label)=lower(?)",
        (would_be, name)))
    if existing:
        raise HttpError(409, f"{existing['label']} is already on the Spec Tree -- "
                             "pick it from the list instead")
    if ctx.conn.execute(
            "SELECT 1 FROM branch_proposals WHERE bike_id=? AND proposed_by=?"
            " AND lower(field_name)=lower(?) AND status='pending'",
            (bike_id, ctx.user["id"], name)).fetchone():
        raise HttpError(409, "you have already proposed that for this bike")
    cur = ctx.conn.execute(
        "INSERT INTO branch_proposals (field_name, category, bike_id, proposed_by, reasoning, value_type)"
        " VALUES (?,?,?,?,?,?)",
        (name, category, bike_id, ctx.user["id"], reasoning, value_type_from(ctx.body)))
    ctx.conn.commit()
    return {"kind": "proposal", "id": cur.lastrowid}


def _field_request_queue(conn, bike_ids=None):
    """Pending requests folded to one row per (bike, field), with who asked."""
    where, args = "", []
    if bike_ids is not None:
        if not bike_ids:
            return []
        where = " WHERE r.bike_id IN (" + ",".join("?" * len(bike_ids)) + ")"
        args = list(bike_ids)
    items = rows(conn.execute(
        "SELECT r.bike_id, r.field_key, f.label, f.category, d.display_name AS bike_name,"
        "       d.year_range, COUNT(*) AS requests, MAX(r.created_at) AS last_requested,"
        "       EXISTS (SELECT 1 FROM bike_managers m WHERE m.bike_id=r.bike_id) AS has_manager"
        " FROM field_requests r"
        " JOIN spec_fields f ON f.field_key = r.field_key"
        " JOIN bike_display d ON d.bike_id = r.bike_id"
        + where.replace("WHERE", "WHERE r.status='pending' AND") +
        ("" if where else " WHERE r.status='pending'") +
        " GROUP BY r.bike_id, r.field_key"
        " ORDER BY requests DESC, last_requested DESC", args))
    for it in items:
        it["asked_by"] = rows(conn.execute(
            "SELECT u.username, u.id AS user_id, r.reasoning, r.created_at FROM field_requests r"
            " JOIN users u ON u.id = r.user_id"
            " WHERE r.bike_id=? AND r.field_key=? AND r.status='pending'"
            " ORDER BY r.created_at", (it["bike_id"], it["field_key"])))
    return items


@route("GET", r"/api/manager/field-requests", role="manager")
def manager_field_requests(ctx):
    """Fields riders want added to your bikes, most-asked first."""
    return {"items": _field_request_queue(ctx.conn, _managed_bike_ids(ctx.conn, ctx.user))}


@route("GET", r"/api/admin/field-requests", role="admin")
def admin_field_requests(ctx):
    """Every pending request. `has_manager` says whether a manager will see it
    on their own dashboard; the ones without are admin's to settle."""
    return {"items": _field_request_queue(ctx.conn)}


# ===========================================================================
# BIKE REQUESTS -- "add my bike"
# ===========================================================================
def _norm(s):
    """"KLR 650", "KLR-650" and "klr650" are one model: letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _bike_request_rows(conn, uid, where, args):
    items = rows(conn.execute(
        "SELECT r.id, r.make, r.model, r.year_from, r.year_to, r.displacement, r.bike_type,"
        "       r.reasoning, r.source_url, r.status, r.admin_note, r.created_at, r.resolved_at,"
        "       r.bike_id, d.display_name AS bike_name, d.year_range AS bike_years,"
        "       u.username AS asked_by, u.id AS asked_by_id,"
        "       (SELECT COUNT(*) FROM bike_request_supporters s WHERE s.request_id=r.id) AS riders,"
        "       EXISTS (SELECT 1 FROM bike_request_supporters s WHERE s.request_id=r.id"
        "               AND s.user_id=?) AS mine"
        " FROM bike_requests r"
        " JOIN users u ON u.id = r.user_id"
        " LEFT JOIN bike_display d ON d.bike_id = r.bike_id"
        + where, [uid] + list(args)))
    for it in items:
        it["mine"] = bool(it["mine"])
    return items


def _existing_bikes_like(conn, make, model, limit=5):
    """Bikes that may already be the one asked for: same make (or any, if the
    make is unknown here), model text in the model code or one of the names."""
    like = f"%{model}%"
    return rows(conn.execute(
        "SELECT d.bike_id, d.display_name, d.year_range FROM bike_display d"
        " WHERE (d.make = ? COLLATE NOCASE OR NOT EXISTS (SELECT 1 FROM bikes b WHERE b.make = ? COLLATE NOCASE))"
        "   AND (d.model_code LIKE ? OR EXISTS (SELECT 1 FROM bike_names n"
        "        WHERE n.bike_id = d.bike_id AND n.name LIKE ?))"
        " ORDER BY d.display_name LIMIT ?", (make, make, like, like, limit)))


@route("GET", r"/api/bike-requests")
def list_bike_requests(ctx):
    """What riders have asked for, and how it went. Public, so the page can
    show "3 riders are waiting on the KLR650" before sign-in; `mine` is only
    ever true signed in. Also the makes the catalogue knows and the kinds of
    bike, for the form."""
    uid = ctx.user["id"] if ctx.user else -1
    pending = _bike_request_rows(ctx.conn, uid,
        " WHERE r.status='pending' ORDER BY riders DESC, r.created_at DESC", [])
    decided = _bike_request_rows(ctx.conn, uid,
        " WHERE r.status<>'pending' ORDER BY r.resolved_at DESC LIMIT 20", [])
    return {
        "pending": pending, "decided": decided,
        "makes": [r[0] for r in ctx.conn.execute(
            "SELECT DISTINCT make FROM bikes ORDER BY make COLLATE NOCASE")],
        "bike_types": questionnaire.load()["bike_types"],
    }


def _clean_bike_request(ctx):
    body = ctx.body
    make = " ".join((body.get("make") or "").split())
    model = " ".join((body.get("model") or "").split())
    if not make or not model:
        raise HttpError(400, "make and model are both needed")
    if len(make) > 60 or len(model) > 80:
        raise HttpError(400, "make or model is too long")

    def year(name):
        v = body.get(name)
        if v in (None, ""):
            return None
        try:
            v = int(v)
        except (TypeError, ValueError):
            raise HttpError(400, f"{name} must be a year")
        if not 1885 <= v <= datetime.now().year + 2:
            raise HttpError(400, f"{name} is not a plausible model year")
        return v
    y0, y1 = year("year_from"), year("year_to")
    if y1 is not None and y0 is None:
        y0, y1 = y1, None
    if y0 is not None and y1 is not None and y1 < y0:
        raise HttpError(400, "year_to cannot be before year_from")

    bike_type = (body.get("bike_type") or "").strip() or None
    if bike_type and bike_type not in questionnaire.load()["bike_types"]:
        raise HttpError(400, "unknown bike type")
    displacement = " ".join((body.get("displacement") or "").split())[:40] or None
    reasoning = (body.get("reasoning") or "").strip() or None
    if reasoning and len(reasoning) > 500:
        raise HttpError(400, "reasoning is limited to 500 characters")
    url = (body.get("source_url") or "").strip() or None
    if url and (len(url) > 300 or not url.lower().startswith(("http://", "https://"))):
        raise HttpError(400, "source_url must be a web address")
    return make, model, y0, y1, bike_type, displacement, reasoning, url


@route("POST", r"/api/bike-requests", role="user")
def create_bike_request(ctx):
    """Ask for a bike. If the same make and model is already asked for and
    still open, this adds the rider's name to that request instead of
    opening a second one -- the count is the people waiting, and admin
    settles them together. If the catalogue already has the bike under that
    make and model, say so rather than file a request for it."""
    make, model, y0, y1, bike_type, displacement, reasoning, url = _clean_bike_request(ctx)

    # already in the catalogue under exactly that make + model (or name)?
    exact = one(ctx.conn.execute(
        "SELECT d.bike_id, d.display_name, d.year_range FROM bike_display d"
        " WHERE d.make = ? COLLATE NOCASE AND (d.model_code = ? COLLATE NOCASE"
        "    OR EXISTS (SELECT 1 FROM bike_names n WHERE n.bike_id = d.bike_id"
        "               AND n.name = ? COLLATE NOCASE))"
        " ORDER BY d.year_start LIMIT 1", (make, model, f"{make} {model}")))
    if exact:
        raise HttpError(409, f"{exact['display_name']}"
                             f"{' (' + exact['year_range'] + ')' if exact['year_range'] else ''}"
                             f" is already in the catalogue -- open it from Browse")

    # the same bike, still open: join it
    for r in rows(ctx.conn.execute(
            "SELECT id, make, model, year_from, year_to FROM bike_requests WHERE status='pending'")):
        if _norm(r["make"]) != _norm(make) or _norm(r["model"]) != _norm(model):
            continue
        # a year range on both sides that cannot overlap is a different bike
        if (y0 is not None and r["year_from"] is not None
                and ((y1 or y0) < r["year_from"] or (r["year_to"] or r["year_from"]) < y0)):
            continue
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO bike_request_supporters (request_id, user_id) VALUES (?,?)",
            (r["id"], ctx.user["id"]))
        ctx.conn.commit()
        riders = ctx.conn.execute(
            "SELECT COUNT(*) FROM bike_request_supporters WHERE request_id=?", (r["id"],)).fetchone()[0]
        return {"ok": True, "kind": "joined", "request_id": r["id"], "riders": riders,
                "already": not cur.rowcount}

    cur = ctx.conn.execute(
        "INSERT INTO bike_requests (user_id, make, model, year_from, year_to, displacement,"
        " bike_type, reasoning, source_url) VALUES (?,?,?,?,?,?,?,?,?)",
        (ctx.user["id"], make, model, y0, y1, displacement, bike_type, reasoning, url))
    ctx.conn.execute(
        "INSERT INTO bike_request_supporters (request_id, user_id) VALUES (?,?)",
        (cur.lastrowid, ctx.user["id"]))
    ctx.conn.commit()
    return {"ok": True, "kind": "new", "request_id": cur.lastrowid, "riders": 1}


@route("POST", r"/api/bike-requests/(\d+)/support", role="user")
def support_bike_request(ctx):
    """Add your name to an open request, or take it off. The rider who filed
    it can take their name off too; the request stays for the others, or
    closes as withdrawn when nobody is left waiting."""
    rid = int(ctx.params[0])
    r = one(ctx.conn.execute("SELECT id, status FROM bike_requests WHERE id=?", (rid,)))
    if not r:
        raise HttpError(404, "no such request")
    if r["status"] != "pending":
        raise HttpError(409, "that request has been decided")
    want = ctx.body.get("on")
    have = ctx.conn.execute(
        "SELECT 1 FROM bike_request_supporters WHERE request_id=? AND user_id=?",
        (rid, ctx.user["id"])).fetchone() is not None
    on = (not have) if want is None else bool(want)
    if on and not have:
        ctx.conn.execute("INSERT INTO bike_request_supporters (request_id, user_id) VALUES (?,?)",
                         (rid, ctx.user["id"]))
    elif not on and have:
        ctx.conn.execute("DELETE FROM bike_request_supporters WHERE request_id=? AND user_id=?",
                         (rid, ctx.user["id"]))
    riders = ctx.conn.execute(
        "SELECT COUNT(*) FROM bike_request_supporters WHERE request_id=?", (rid,)).fetchone()[0]
    withdrawn = False
    if riders == 0:
        ctx.conn.execute("DELETE FROM bike_requests WHERE id=?", (rid,))
        withdrawn = True
    ctx.conn.commit()
    return {"ok": True, "on": on, "riders": riders, "withdrawn": withdrawn}


@route("GET", r"/api/admin/bike-requests", role="admin")
def admin_bike_requests(ctx):
    """Every open request, most riders first, with the bikes already in the
    catalogue that might be the same machine -- so "add it" is never done
    over a duplicate that only differs by a name."""
    items = _bike_request_rows(ctx.conn, ctx.user["id"],
        " WHERE r.status='pending' ORDER BY riders DESC, r.created_at", [])
    for it in items:
        it["supporters"] = rows(ctx.conn.execute(
            "SELECT u.username, u.id AS user_id FROM bike_request_supporters s"
            " JOIN users u ON u.id = s.user_id WHERE s.request_id=? ORDER BY s.created_at",
            (it["id"],)))
        it["maybe"] = _existing_bikes_like(ctx.conn, it["make"], it["model"])
    return {"items": items, "bike_types": questionnaire.load()["bike_types"]}


@route("POST", r"/api/admin/bike-requests/(\d+)/decide", role="admin")
def decide_bike_request(ctx):
    """Add the bike, point the request at one that already exists, or decline.

    "added" with no bike_id creates the bike from the request -- make, model
    as the model code, the years -- with any of those overridden from the
    body (the rider's "CB919" becomes the catalogue's "CB900F", say). The
    years are required to create: a bike is a make + model + years, and a
    guessed year is a wrong one. With a bike_id, the request is settled as
    that existing bike, nothing is created. Every rider waiting is answered
    either way; the note is what they read.
    """
    rid = int(ctx.params[0])
    r = one(ctx.conn.execute("SELECT * FROM bike_requests WHERE id=?", (rid,)))
    if not r:
        raise HttpError(404, "no such request")
    if r["status"] != "pending":
        raise HttpError(409, "that request has been decided")
    status = ctx.field("status")
    if status not in ("added", "declined"):
        raise HttpError(400, "status must be added or declined")
    note = (ctx.body.get("note") or "").strip() or None
    bike_id, created = None, False
    label = f'{r["make"]} {r["model"]}'
    if status == "added":
        if ctx.body.get("bike_id"):
            bike_id = int(ctx.body["bike_id"])
            if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike_id,)).fetchone():
                raise HttpError(404, "no such bike")
        else:
            make = " ".join((ctx.body.get("make") or r["make"]).split())
            model_code = " ".join((ctx.body.get("model_code") or r["model"]).split())
            try:
                y0 = int(ctx.body.get("year_start") or r["year_from"] or 0)
                y1 = int(ctx.body.get("year_end") or r["year_to"] or y0)
            except (TypeError, ValueError):
                raise HttpError(400, "years must be numbers")
            if not y0:
                raise HttpError(400, "the request has no years -- give the bike a start year")
            bike_type = ctx.body.get("bike_type") or r["bike_type"]
            if bike_type and bike_type not in questionnaire.load()["bike_types"]:
                raise HttpError(400, "unknown bike type")
            bike_id, _ = _create_bike(ctx.conn, make, model_code, y0, y1, bike_type,
                                      ctx.body.get("name"))
            created = True
    riders = ctx.conn.execute(
        "SELECT COUNT(*) FROM bike_request_supporters WHERE request_id=?", (rid,)).fetchone()[0]
    ctx.conn.execute(
        "UPDATE bike_requests SET status=?, bike_id=?, decided_by=?, admin_note=?,"
        " resolved_at=datetime('now') WHERE id=?",
        (status, bike_id, ctx.user["id"], note, rid))
    log_action(ctx, "bikerequest.decide",
               (f'Added "{label}" as bike #{bike_id}' if created
                else f'Matched "{label}" to bike #{bike_id}' if bike_id
                else f'Declined "{label}"') + f" for {riders} rider(s)",
               target=str(rid), detail={"request_id": rid, "status": status, "bike_id": bike_id,
                                        "created": created, "note": note, "riders": riders})
    ctx.conn.commit()
    return {"ok": True, "status": status, "bike_id": bike_id, "created": created, "riders": riders}


# ===========================================================================
# MANAGERS' BOARD and DIRECT MESSAGES
# ===========================================================================
BOARD_TITLE_MAX, BOARD_POST_MAX, DM_MAX = 120, 4000, 2000


def _is_staff(user):
    return user is not None and user["role"] in ("manager", "admin")


def _clean_text(value, limit, what):
    text = (value or "").replace("\r\n", "\n").strip()
    if not text:
        raise HttpError(400, f"{what} cannot be empty")
    if len(text) > limit:
        raise HttpError(400, f"{what} is limited to {limit} characters")
    return text


def _mark_read(conn, tid, uid):
    """Read up to the thread's newest post."""
    conn.execute(
        "INSERT OR REPLACE INTO board_reads (thread_id, user_id, read_post_id, read_at)"
        " VALUES (?,?,(SELECT last_post_id FROM board_threads WHERE id=?),datetime('now'))",
        (tid, uid, tid))


def _thread_or_404(conn, tid):
    t = one(conn.execute(
        "SELECT t.*, u.username AS author, d.display_name AS bike_name, d.year_range AS bike_years"
        " FROM board_threads t JOIN users u ON u.id = t.author_id"
        " LEFT JOIN bike_display d ON d.bike_id = t.bike_id WHERE t.id=?", (tid,)))
    if not t:
        raise HttpError(404, "no such thread")
    return t


@route("GET", r"/api/board/members", role="manager")
def board_members(ctx):
    """Who is on the board: every manager with the bikes they manage, and
    admin. The people a message can go to."""
    people = rows(ctx.conn.execute(
        "SELECT u.id, u.username, u.display_name, u.role,"
        "       (SELECT GROUP_CONCAT(d.display_name, '|') FROM bike_managers m"
        "         JOIN bike_display d ON d.bike_id = m.bike_id WHERE m.user_id = u.id) AS bikes"
        " FROM users u"
        " WHERE u.role = 'admin' OR EXISTS (SELECT 1 FROM bike_managers m WHERE m.user_id = u.id)"
        " ORDER BY u.role = 'admin' DESC, u.username COLLATE NOCASE"))
    for p in people:
        p["bikes"] = [b for b in (p.pop("bikes") or "").split("|") if b]
        p["me"] = p["id"] == ctx.user["id"]
    return {"people": people}


@route("GET", r"/api/board/threads", role="manager")
def board_threads(ctx):
    """Every thread, pinned ones first, then by latest post. `unread` is
    per reader: posts since they last opened it, or never opened."""
    q = (ctx.arg("q") or "").strip()
    where, args = "", []
    if q:
        where = (" WHERE (t.title LIKE ? OR EXISTS (SELECT 1 FROM board_posts p"
                 "   WHERE p.thread_id = t.id AND p.body LIKE ?))")
        args = [f"%{q}%", f"%{q}%"]
    items = rows(ctx.conn.execute(
        "SELECT t.id, t.title, t.bike_id, t.pinned, t.locked, t.created_at, t.last_post_at,"
        "       u.username AS author, u.id AS author_id,"
        "       d.display_name AS bike_name, d.year_range AS bike_years,"
        "       (SELECT COUNT(*) FROM board_posts p WHERE p.thread_id = t.id) AS posts,"
        "       (SELECT lu.username FROM board_posts p JOIN users lu ON lu.id = p.author_id"
        "         WHERE p.thread_id = t.id ORDER BY p.created_at DESC, p.id DESC LIMIT 1) AS last_by,"
        "       COALESCE((SELECT r.read_post_id FROM board_reads r WHERE r.thread_id = t.id AND r.user_id = ?),"
        "                0) < t.last_post_id AS unread"
        " FROM board_threads t JOIN users u ON u.id = t.author_id"
        " LEFT JOIN bike_display d ON d.bike_id = t.bike_id"
        + where + " ORDER BY t.pinned DESC, t.last_post_at DESC", [ctx.user["id"]] + args))
    for it in items:
        it["unread"] = bool(it["unread"])
        it["pinned"] = bool(it["pinned"])
        it["locked"] = bool(it["locked"])
    return {"threads": items}


@route("POST", r"/api/board/threads", role="manager")
def board_new_thread(ctx):
    title = _clean_text(ctx.body.get("title"), BOARD_TITLE_MAX, "title")
    body = _clean_text(ctx.body.get("body"), BOARD_POST_MAX, "post")
    bike_id = ctx.body.get("bike_id") or None
    if bike_id is not None:
        bike_id = int(bike_id)
        if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike_id,)).fetchone():
            raise HttpError(404, "no such bike")
    cur = ctx.conn.execute(
        "INSERT INTO board_threads (author_id, title, bike_id) VALUES (?,?,?)",
        (ctx.user["id"], title, bike_id))
    tid = cur.lastrowid
    pid = ctx.conn.execute("INSERT INTO board_posts (thread_id, author_id, body) VALUES (?,?,?)",
                           (tid, ctx.user["id"], body)).lastrowid
    ctx.conn.execute("UPDATE board_threads SET last_post_id=? WHERE id=?", (pid, tid))
    _mark_read(ctx.conn, tid, ctx.user["id"])
    ctx.conn.commit()
    return {"ok": True, "thread_id": tid}


@route("GET", r"/api/board/threads/(\d+)", role="manager")
def board_thread(ctx):
    """The thread and every post in it. Opening it marks it read."""
    tid = int(ctx.params[0])
    t = _thread_or_404(ctx.conn, tid)
    posts = rows(ctx.conn.execute(
        "SELECT p.id, p.body, p.created_at, p.edited_at, u.username AS author, u.id AS author_id,"
        "       u.role AS author_role, u.retired_tier AS author_retired, u.founder AS author_founder"
        " FROM board_posts p JOIN users u ON u.id = p.author_id"
        " WHERE p.thread_id=? ORDER BY p.created_at, p.id", (tid,)))
    first = posts[0]["id"] if posts else None
    for p in posts:
        p["mine"] = p["author_id"] == ctx.user["id"]
        p["first"] = p["id"] == first
    _mark_read(ctx.conn, tid, ctx.user["id"])
    ctx.conn.commit()
    t["pinned"], t["locked"] = bool(t["pinned"]), bool(t["locked"])
    t["mine"] = t["author_id"] == ctx.user["id"]
    return {"thread": t, "posts": posts}


@route("POST", r"/api/board/threads/(\d+)/posts", role="manager")
def board_reply(ctx):
    tid = int(ctx.params[0])
    t = _thread_or_404(ctx.conn, tid)
    if t["locked"] and ctx.user["role"] != "admin":
        raise HttpError(409, "this thread is locked")
    body = _clean_text(ctx.body.get("body"), BOARD_POST_MAX, "post")
    cur = ctx.conn.execute(
        "INSERT INTO board_posts (thread_id, author_id, body) VALUES (?,?,?)",
        (tid, ctx.user["id"], body))
    ctx.conn.execute(
        "UPDATE board_threads SET last_post_id=?,"
        "  last_post_at = (SELECT created_at FROM board_posts WHERE id=?) WHERE id=?",
        (cur.lastrowid, cur.lastrowid, tid))
    _mark_read(ctx.conn, tid, ctx.user["id"])   # your own reply is not unread to you
    ctx.conn.commit()
    return {"ok": True, "post_id": cur.lastrowid}


@route("PATCH", r"/api/board/posts/(\d+)", role="manager")
def board_edit_post(ctx):
    """Your own words, or admin's call on anyone's."""
    pid = int(ctx.params[0])
    p = one(ctx.conn.execute("SELECT id, author_id, thread_id FROM board_posts WHERE id=?", (pid,)))
    if not p:
        raise HttpError(404, "no such post")
    if p["author_id"] != ctx.user["id"] and ctx.user["role"] != "admin":
        raise HttpError(403, "not your post")
    body = _clean_text(ctx.body.get("body"), BOARD_POST_MAX, "post")
    ctx.conn.execute("UPDATE board_posts SET body=?, edited_at=datetime('now') WHERE id=?", (body, pid))
    ctx.conn.commit()
    return {"ok": True}


@route("DELETE", r"/api/board/posts/(\d+)", role="manager")
def board_delete_post(ctx):
    """A reply can go -- yours, or any as admin. The opening post is the
    thread; deleting that is deleting the thread, below."""
    pid = int(ctx.params[0])
    p = one(ctx.conn.execute("SELECT id, author_id, thread_id FROM board_posts WHERE id=?", (pid,)))
    if not p:
        raise HttpError(404, "no such post")
    if p["author_id"] != ctx.user["id"] and ctx.user["role"] != "admin":
        raise HttpError(403, "not your post")
    first = ctx.conn.execute(
        "SELECT id FROM board_posts WHERE thread_id=? ORDER BY created_at, id LIMIT 1",
        (p["thread_id"],)).fetchone()[0]
    if first == pid:
        raise HttpError(409, "that is the opening post -- delete the thread instead")
    ctx.conn.execute("DELETE FROM board_posts WHERE id=?", (pid,))
    ctx.conn.execute(
        "UPDATE board_threads SET"
        "  last_post_at = (SELECT MAX(created_at) FROM board_posts WHERE thread_id=?),"
        "  last_post_id = (SELECT MAX(id) FROM board_posts WHERE thread_id=?)"
        " WHERE id=?", (p["thread_id"], p["thread_id"], p["thread_id"]))
    ctx.conn.commit()
    return {"ok": True}


@route("DELETE", r"/api/board/threads/(\d+)", role="manager")
def board_delete_thread(ctx):
    """The author can take back a thread nobody has replied to; once others
    have written in it, it is theirs too, and only admin removes it."""
    tid = int(ctx.params[0])
    t = _thread_or_404(ctx.conn, tid)
    replies = ctx.conn.execute(
        "SELECT COUNT(*) FROM board_posts WHERE thread_id=? AND author_id<>?",
        (tid, t["author_id"])).fetchone()[0]
    if ctx.user["role"] != "admin" and (t["author_id"] != ctx.user["id"] or replies):
        raise HttpError(403, "only admin can remove a thread others have replied to")
    ctx.conn.execute("DELETE FROM board_threads WHERE id=?", (tid,))
    if ctx.user["role"] == "admin" and t["author_id"] != ctx.user["id"]:
        log_action(ctx, "board.remove", f'Removed thread "{t["title"]}" by {t["author"]}',
                   target=str(tid), destructive=True,
                   detail={"thread_id": tid, "title": t["title"], "author": t["author"]})
    ctx.conn.commit()
    return {"ok": True}


@route("POST", r"/api/board/threads/(\d+)/(pin|lock)", role="admin")
def board_pin_or_lock(ctx):
    tid, what = int(ctx.params[0]), ctx.params[1]
    t = _thread_or_404(ctx.conn, tid)
    want = ctx.body.get("on")
    col = "pinned" if what == "pin" else "locked"
    new = (0 if t[col] else 1) if want is None else (1 if want else 0)
    ctx.conn.execute(f"UPDATE board_threads SET {col}=? WHERE id=?", (new, tid))
    ctx.conn.commit()
    return {"ok": True, col: bool(new)}


# ---- direct messages ------------------------------------------------------
def _dm_pair(a, b):
    return (a, b) if a < b else (b, a)


def _dm_other_or_404(conn, me, other_id):
    other = one(conn.execute("SELECT id, username, display_name, role FROM users WHERE id=?", (other_id,)))
    if not other:
        raise HttpError(404, "no such person")
    if other["id"] == me["id"]:
        raise HttpError(400, "that is you")
    if not (other["role"] == "admin" or conn.execute(
            "SELECT 1 FROM bike_managers WHERE user_id=?", (other["id"],)).fetchone()):
        raise HttpError(403, "messages are between managers and admin")
    return other


@route("GET", r"/api/messages", role="manager")
def dm_list(ctx):
    """Your conversations, latest first, with what was said last and how
    many of theirs you have not opened."""
    me = ctx.user["id"]
    convs = rows(ctx.conn.execute(
        "SELECT c.id, c.last_message_at,"
        "       CASE WHEN c.user_a = ? THEN c.user_b ELSE c.user_a END AS other_id,"
        "       (SELECT m.body FROM dm_messages m WHERE m.conversation_id = c.id"
        "         ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_body,"
        "       (SELECT m.sender_id FROM dm_messages m WHERE m.conversation_id = c.id"
        "         ORDER BY m.created_at DESC, m.id DESC LIMIT 1) AS last_sender,"
        "       (SELECT COUNT(*) FROM dm_messages m WHERE m.conversation_id = c.id"
        "         AND m.sender_id <> ? AND m.read_at IS NULL) AS unread"
        " FROM dm_conversations c WHERE c.user_a = ? OR c.user_b = ?"
        " ORDER BY c.last_message_at DESC", (me, me, me, me)))
    for c in convs:
        u = one(ctx.conn.execute("SELECT username, display_name, role FROM users WHERE id=?",
                                 (c["other_id"],)))
        c["other"] = u["username"] if u else "(gone)"
        c["other_role"] = u["role"] if u else None
        c["last_mine"] = c["last_sender"] == me
        c["preview"] = (c["last_body"] or "")[:90]
        c.pop("last_body")
    return {"conversations": convs}


@route("GET", r"/api/messages/with/(\d+)", role="manager")
def dm_thread(ctx):
    """The conversation with one person; opening it marks their messages
    read. Nothing is created by looking."""
    other = _dm_other_or_404(ctx.conn, ctx.user, int(ctx.params[0]))
    a, b = _dm_pair(ctx.user["id"], other["id"])
    conv = one(ctx.conn.execute(
        "SELECT id FROM dm_conversations WHERE user_a=? AND user_b=?", (a, b)))
    messages = []
    if conv:
        messages = rows(ctx.conn.execute(
            "SELECT m.id, m.sender_id, m.body, m.created_at, m.read_at"
            " FROM dm_messages m WHERE m.conversation_id=? ORDER BY m.created_at, m.id", (conv["id"],)))
        for m in messages:
            m["mine"] = m["sender_id"] == ctx.user["id"]
        ctx.conn.execute(
            "UPDATE dm_messages SET read_at = datetime('now')"
            " WHERE conversation_id=? AND sender_id<>? AND read_at IS NULL", (conv["id"], ctx.user["id"]))
        ctx.conn.commit()
    return {"other": other, "messages": messages}


@route("POST", r"/api/messages/with/(\d+)", role="manager")
def dm_send(ctx):
    other = _dm_other_or_404(ctx.conn, ctx.user, int(ctx.params[0]))
    body = _clean_text(ctx.body.get("body"), DM_MAX, "message")
    a, b = _dm_pair(ctx.user["id"], other["id"])
    ctx.conn.execute("INSERT OR IGNORE INTO dm_conversations (user_a, user_b) VALUES (?,?)", (a, b))
    cid = ctx.conn.execute(
        "SELECT id FROM dm_conversations WHERE user_a=? AND user_b=?", (a, b)).fetchone()[0]
    cur = ctx.conn.execute(
        "INSERT INTO dm_messages (conversation_id, sender_id, body) VALUES (?,?,?)",
        (cid, ctx.user["id"], body))
    ctx.conn.execute(
        "UPDATE dm_conversations SET last_message_at = (SELECT created_at FROM dm_messages WHERE id=?)"
        " WHERE id=?", (cur.lastrowid, cid))
    ctx.conn.commit()
    return {"ok": True, "message_id": cur.lastrowid, "conversation_id": cid}


def _unread_counts(conn, user):
    """For the nav: threads with posts you have not seen, messages you have
    not opened. Staff only; a rider has neither."""
    if not _is_staff(user):
        return None
    board = conn.execute(
        "SELECT COUNT(*) FROM board_threads t"
        " WHERE COALESCE((SELECT r.read_post_id FROM board_reads r WHERE r.thread_id = t.id AND r.user_id = ?),"
        "                0) < t.last_post_id", (user["id"],)).fetchone()[0]
    messages = conn.execute(
        "SELECT COUNT(*) FROM dm_messages m JOIN dm_conversations c ON c.id = m.conversation_id"
        " WHERE (c.user_a = ? OR c.user_b = ?) AND m.sender_id <> ? AND m.read_at IS NULL",
        (user["id"], user["id"], user["id"])).fetchone()[0]
    return {"board": board, "messages": messages}


@route("POST", r"/api/bikes/(\d+)/field-requests/([a-z0-9_]+)/decide", role="manager")
def decide_field_request(ctx):
    """Add the field to the bike, or decline. The bike's manager, or admin.

    Adding puts an empty, pending spec row on the bike -- the same thing the
    questionnaire or admin's apply does -- or, where the bike already has the
    row staged offline, puts it online. Either way every rider's request for
    that field resolves at once, because the field is there now for all of
    them.
    """
    bike_id, key = int(ctx.params[0]), ctx.params[1]
    require_manages(ctx.conn, ctx.user, bike_id)
    status = ctx.field("status")
    if status not in ("added", "declined"):
        raise HttpError(400, "status must be added or declined")
    field = one(ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field")
    note = (ctx.body.get("note") or "").strip() or None
    added = False
    if status == "added":
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
            " VALUES (?,?,NULL,'pending')", (bike_id, key))
        added = bool(cur.rowcount)
        if not added:
            # the bike has the row but staged offline: put it online
            cur = ctx.conn.execute(
                "UPDATE specs SET paused=0, paused_by=NULL, paused_at=NULL"
                " WHERE bike_id=? AND field_key=? AND paused=1", (bike_id, key))
            added = cur.rowcount > 0
    resolved = ctx.conn.execute(
        "UPDATE field_requests SET status=?, decided_by=?, admin_note=?,"
        " resolved_at=datetime('now') WHERE bike_id=? AND field_key=? AND status='pending'",
        (status, ctx.user["id"], note, bike_id, key)).rowcount
    if ctx.user["role"] == "admin":
        log_action(ctx, "fieldrequest.decide",
                   f'{"Added" if status == "added" else "Declined"} "{field["label"]}"'
                   f" on bike #{bike_id} for {resolved} request(s)",
                   target=key, detail={"bike_id": bike_id, "status": status, "note": note,
                                       "resolved": resolved})
    ctx.conn.commit()
    return {"ok": True, "added": added, "resolved": resolved}


# ===========================================================================
# USER PROFILES
# ===========================================================================
# ===========================================================================
# MANAGER STANDING -- tiers and badges, computed from the record
# ===========================================================================
# A tier is read off what a manager has done, never stored (Gold also needs
# admin's confirmation, which is the one stored bit). It changes how a
# manager is SHOWN -- the glyph, the pill, their place in a list -- and
# never what they can do; permissions stay with the three roles.
TIER_RULES = {
    "silver": {"specs": 100, "confirmed_share": 60, "flags": 10, "median_days": 7.0},
    "gold":   {"specs": 400, "confirmed_share": 85, "flags": 50, "median_days": 3.0},
}
WRONG_RATE_CAP = 5.0        # % of a manager's values later fixed after a flag: above this, Bronze
MEDIAN_MIN_FLAGS = 5        # a median of fewer than this says nothing

BADGE_RULES = [
    ("founding",      "Founding Manager", "Given by admin to the managers who built the site"),
    ("first_hundred", "First Hundred",    "100 spec values entered"),
    ("manual_hunter", "Manual Hunter",    "50 values straight from the manual (Confirmed)"),
    ("second_saddle", "Second Saddle",    "Manages two or more bikes"),
    ("quick_draw",    "Quick Draw",       "10+ flags resolved, half of them within a day"),
    ("clean_sheet",   "Clean Sheet",      "20+ values entered and none overturned in 90 days"),
    ("wire_wizard",   "Wire Wizard",      "25 wire-colour values entered"),
    ("full_sheet",    "Full Sheet",       "A managed bike with every spec filled"),
]


def manager_standing(conn, user_id):
    """Everything the tier and the badges are decided from, plus the
    decision, for one person. Cheap enough to compute on demand."""
    u = one(conn.execute("SELECT id, username, role, gold_confirmed, founder, retired_tier, retired_at,"
                         " created_at FROM users WHERE id=?", (user_id,)))
    if not u:
        raise HttpError(404, "user not found")
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]
    bikes = rows(conn.execute(
        "SELECT d.bike_id, d.display_name, d.year_range, p.fields_triggered, p.specs_filled, p.specs_needed,"
        "       (b.lead_manager_id = m.user_id) AS lead"
        " FROM bike_managers m JOIN bike_display d ON d.bike_id = m.bike_id"
        " JOIN bikes b ON b.id = m.bike_id"
        " JOIN bike_spec_progress p ON p.bike_id = m.bike_id"
        " WHERE m.user_id=? ORDER BY d.display_name", (user_id,)))
    for b in bikes:
        b["lead"] = bool(b["lead"])
    lead_of = rows(conn.execute(
        "SELECT d.bike_id, d.display_name, d.year_range FROM bikes b JOIN bike_display d ON d.bike_id = b.id"
        " WHERE b.lead_manager_id=? ORDER BY d.display_name", (user_id,)))
    specs = q("SELECT COUNT(*) FROM specs WHERE entered_by=? AND value IS NOT NULL AND TRIM(value)<>''", user_id)
    confirmed = q("SELECT COUNT(*) FROM specs WHERE entered_by=? AND value IS NOT NULL AND TRIM(value)<>''"
                  " AND confidence='confirmed'", user_id)
    wire = q("SELECT COUNT(*) FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
             " WHERE s.entered_by=? AND s.value IS NOT NULL AND f.value_type='wire_color'", user_id)
    flags = q("SELECT COUNT(*) FROM value_flags WHERE resolved_by=? AND status IN ('fixed','dismissed')", user_id)
    upheld = q("SELECT COUNT(*) FROM value_flags WHERE old_entered_by=? AND status='fixed'", user_id)
    upheld_90 = q("SELECT COUNT(*) FROM value_flags WHERE old_entered_by=? AND status='fixed'"
                  " AND resolved_at >= datetime('now','-90 days')", user_id)
    helped = (q("SELECT COUNT(*) FROM spec_votes v JOIN specs s ON s.id = v.spec_id WHERE s.entered_by=?", user_id)
              + q("SELECT COUNT(*) FROM alternate_votes v JOIN spec_alternates a ON a.id = v.alternate_id"
                  " WHERE a.submitted_by=?", user_id))
    waits = sorted(r[0] for r in conn.execute(
        "SELECT julianday(resolved_at) - julianday(created_at) FROM value_flags"
        " WHERE resolved_by=? AND resolved_at IS NOT NULL", (user_id,)))
    median = None
    if len(waits) >= MEDIAN_MIN_FLAGS:
        mid = len(waits) // 2
        median = round(waits[mid] if len(waits) % 2 else (waits[mid - 1] + waits[mid]) / 2, 2)

    share = round(100 * confirmed / specs, 1) if specs else None
    wrong = round(100 * upheld / specs, 1) if specs else 0.0
    is_manager = bool(bikes) or u["role"] == "admin"

    def meets(rule):
        return (specs >= rule["specs"] and (share or 0) >= rule["confirmed_share"]
                and flags >= rule["flags"] and median is not None and median <= rule["median_days"])
    # what the numbers say, whether or not they hold a bike today: the live
    # tier while they manage, the tier frozen at retirement otherwise
    earned = "bronze"
    if wrong <= WRONG_RATE_CAP:
        if meets(TIER_RULES["silver"]):
            earned = "silver"
        if meets(TIER_RULES["gold"]) and u["gold_confirmed"]:
            earned = "gold"
    tier = None
    if u["role"] == "admin":
        tier = "admin"
    elif is_manager:
        tier = earned
    gold_eligible = is_manager and u["role"] != "admin" and wrong <= WRONG_RATE_CAP and meets(TIER_RULES["gold"])

    have = {
        "founding":      bool(u["founder"]),
        "first_hundred": specs >= 100,
        "manual_hunter": confirmed >= 50,
        "second_saddle": len(bikes) >= 2,
        "quick_draw":    flags >= 10 and median is not None and median <= 1.0,
        "clean_sheet":   specs >= 20 and upheld_90 == 0,
        "wire_wizard":   wire >= 25,
        "full_sheet":    any(b["fields_triggered"] and not b["specs_needed"] for b in bikes),
    }
    badges = [{"key": k, "name": n, "rule": r, "earned": bool(have.get(k))} for k, n, r in BADGE_RULES]

    return {
        "user_id": u["id"], "username": u["username"], "role": u["role"],
        "is_manager": is_manager, "tier": tier, "founder": bool(u["founder"]),
        "earned_tier": earned, "lead_of": lead_of,
        "retired": ({"tier": u["retired_tier"], "at": u["retired_at"]} if u["retired_tier"] else None),
        "bikes": bikes,
        "stats": {"specs_entered": specs, "confirmed": confirmed, "confirmed_share": share,
                  "flags_resolved": flags, "median_response_days": median,
                  "wrong_specs": upheld, "wrong_rate": wrong, "helped": helped, "wire_values": wire},
        "badges": badges,
        "gold_confirmed": bool(u["gold_confirmed"]), "gold_eligible": gold_eligible,
        "rules": {"silver": TIER_RULES["silver"], "gold": TIER_RULES["gold"],
                  "wrong_rate_cap": WRONG_RATE_CAP, "median_min_flags": MEDIAN_MIN_FLAGS},
    }


@route("GET", r"/api/users/(\d+)/standing")
def user_standing(ctx):
    """A member's tier, the numbers behind it, and their badges. Public:
    the tier is on every bike's header and the profile explains it."""
    return manager_standing(ctx.conn, int(ctx.params[0]))


@route("POST", r"/api/admin/users/(\d+)/founder", role="admin")
def admin_set_founder(ctx):
    """The Founding Manager badge: admin's to give, and to take back."""
    uid = int(ctx.params[0])
    u = one(ctx.conn.execute("SELECT username FROM users WHERE id=?", (uid,)))
    if not u:
        raise HttpError(404, "no such user")
    on = bool(ctx.body.get("founder", True))
    ctx.conn.execute("UPDATE users SET founder=? WHERE id=?", (1 if on else 0, uid))
    log_action(ctx, "user.founder" if on else "user.unfounder",
               f"{'Gave' if on else 'Took back'} the Founding Manager badge: {u['username']}", target=str(uid))
    ctx.conn.commit()
    return {"ok": True, "founder": on}


@route("POST", r"/api/admin/bikes/(\d+)/lead", role="admin")
def admin_set_lead(ctx):
    """Name a bike's lead manager by hand -- the first manager is set
    automatically; this is for putting it right."""
    bike_id = int(ctx.params[0])
    uid = int(ctx.field("user_id"))
    if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike_id,)).fetchone():
        raise HttpError(404, "bike not found")
    u = one(ctx.conn.execute("SELECT username FROM users WHERE id=?", (uid,)))
    if not u:
        raise HttpError(404, "user not found")
    ctx.conn.execute("UPDATE bikes SET lead_manager_id=?, lead_since=COALESCE(lead_since, datetime('now'))"
                     " WHERE id=?", (uid, bike_id))
    name = ctx.conn.execute("SELECT display_name FROM bike_display WHERE bike_id=?", (bike_id,)).fetchone()[0]
    log_action(ctx, "bike.lead", f"Named {u['username']} lead manager of {name}", target=str(bike_id))
    ctx.conn.commit()
    return {"ok": True}


@route("POST", r"/api/admin/users/(\d+)/retire", role="admin")
def admin_retire(ctx):
    """A manager steps back. The tier they leave with is written down now
    -- a live tier is computed from what you are doing, and a retired
    manager is doing nothing -- and their bikes must already be handed on.
    Reversed with retired=false; the tier goes live again when a bike is."""
    uid = int(ctx.params[0])
    st = manager_standing(ctx.conn, uid)
    on = bool(ctx.body.get("retired", True))
    if on:
        if st["role"] == "admin":
            raise HttpError(400, "admin does not retire from managing")
        if st["bikes"]:
            raise HttpError(409, f"{st['username']} still manages {len(st['bikes'])} bike(s) -- hand them on first")
        tier = st["earned_tier"]
        ctx.conn.execute("UPDATE users SET retired_tier=?, retired_at=datetime('now') WHERE id=?", (tier, uid))
        log_action(ctx, "user.retire", f"{st['username']} retired as {TIER_LABEL.get(tier, tier)}", target=str(uid))
    else:
        ctx.conn.execute("UPDATE users SET retired_tier=NULL, retired_at=NULL WHERE id=?", (uid,))
        log_action(ctx, "user.unretire", f"{st['username']} is back from retirement", target=str(uid))
    ctx.conn.commit()
    return {"ok": True, "retired": manager_standing(ctx.conn, uid)["retired"]}


TIER_LABEL = {"bronze": "Bronze", "silver": "Silver", "gold": "Gold"}


@route("POST", r"/api/admin/users/(\d+)/gold", role="admin")
def admin_confirm_gold(ctx):
    """Gold is the one tier that needs a person's say-so on top of the
    numbers. Confirm it, or take the confirmation back."""
    uid = int(ctx.params[0])
    st = manager_standing(ctx.conn, uid)
    on = bool(ctx.body.get("confirmed", True))
    if on and not st["gold_eligible"]:
        raise HttpError(409, f"{st['username']} does not meet the Gold numbers yet")
    ctx.conn.execute("UPDATE users SET gold_confirmed=? WHERE id=?", (1 if on else 0, uid))
    log_action(ctx, "user.gold" if on else "user.ungold",
               f"{'Confirmed' if on else 'Withdrew'} Gold for {st['username']}", target=str(uid))
    ctx.conn.commit()
    return {"ok": True, "tier": manager_standing(ctx.conn, uid)["tier"]}


@route("GET", r"/api/users/(\d+)/profile")
def user_profile(ctx):
    p = one(ctx.conn.execute(
        "SELECT * FROM user_profile_stats WHERE user_id=?", (int(ctx.params[0]),)))
    if not p:
        raise HttpError(404, "user not found")
    return p


@route("POST", r"/api/users/(\d+)/flags", role="user")
def flag_user(ctx):
    target = int(ctx.params[0])
    if target == ctx.user["id"]:
        raise HttpError(400, "you cannot flag yourself")
    if not ctx.conn.execute("SELECT 1 FROM users WHERE id=?", (target,)).fetchone():
        raise HttpError(404, "user not found")
    reason = ctx.field("reason")
    if reason not in ("spam", "harassment", "bad-faith", "fraud",
                      "inappropriate", "other"):
        raise HttpError(400, "unknown reason")
    detail = ctx.field("detail", required=False)
    if detail and len(detail) > 150:
        raise HttpError(400, "detail is limited to 150 characters")
    cur = ctx.conn.execute(
        "INSERT INTO user_flags (flagged_user, flagged_by, reason, detail)"
        " VALUES (?,?,?,?)", (target, ctx.user["id"], reason, detail))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


# ===========================================================================
# NOTES ON A SPEC
# ===========================================================================
NOTE_MAX = 500


@route("PATCH", r"/api/bikes/(\d+)/specs/([a-z0-9_]+)/note", role="manager")
def set_spec_note(ctx):
    """A note on this spec, on this bike: the manager's sentence beside the
    value -- "measure cold", "later bikes use a longer bolt". One per spec
    per bike; writing again replaces it, and an empty body clears it."""
    bike_id, key = int(ctx.params[0]), ctx.params[1]
    require_manages(ctx.conn, ctx.user, bike_id)
    if not ctx.conn.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key=?",
                            (bike_id, key)).fetchone():
        raise HttpError(404, "this bike does not have that spec")
    body = (ctx.body.get("body") or "").strip()
    if not body:
        ctx.conn.execute("DELETE FROM spec_notes WHERE bike_id=? AND field_key=?", (bike_id, key))
        ctx.conn.commit()
        return {"ok": True, "note": None}
    if len(body) > NOTE_MAX:
        raise HttpError(400, f"a note is limited to {NOTE_MAX} characters")
    # a partial unique index cannot be an ON CONFLICT target, so: update, else insert
    changed = ctx.conn.execute(
        "UPDATE spec_notes SET body=?, written_by=?, updated_at=datetime('now')"
        " WHERE bike_id=? AND field_key=?", (body, ctx.user["id"], bike_id, key)).rowcount
    if not changed:
        ctx.conn.execute(
            "INSERT INTO spec_notes (bike_id, field_key, body, written_by) VALUES (?,?,?,?)",
            (bike_id, key, body, ctx.user["id"]))
    notify_admin(ctx, "note", bike_id,
                 f'{ctx.user["username"]} wrote a note on {key} for bike #{bike_id}', {"body": body})
    ctx.conn.commit()
    return {"ok": True, "note": _note_of(ctx.conn, bike_id, key)}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/example", role="admin")
def set_field_example(ctx):
    """What a good value looks like for this field. It shows as the example
    in the entry box on every bike -- "e.g. K&N KN-145" -- so a part number
    arrives in the shape the next reader expects. Never saved as a value:
    an empty box stays empty."""
    key = ctx.params[0]
    field = one(ctx.conn.execute("SELECT label, value_type FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field")
    if field["value_type"] != "text":
        raise HttpError(409, f"{field['label']} is picked from a list, not typed -- "
                             "an example would never be seen")
    example = (ctx.body.get("example") or "").strip() or None
    if example and len(example) > 120:
        raise HttpError(400, "an example is limited to 120 characters")
    ctx.conn.execute("UPDATE spec_fields SET example=? WHERE field_key=?", (example, key))
    log_action(ctx, "field.example",
               f'Example for "{field["label"]}": {example}' if example
               else f'Cleared the example for "{field["label"]}"',
               target=key, detail={"example": example})
    ctx.conn.commit()
    return {"ok": True, "example": example}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/note", role="admin")
def set_field_note(ctx):
    """A note on the FIELD: it shows on every bike that carries it. For what
    is true everywhere -- how a figure is measured, a warning about the
    manual. A bike's own note sits under it, never instead of it."""
    key = ctx.params[0]
    field = one(ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field")
    body = (ctx.body.get("body") or "").strip()
    if not body:
        ctx.conn.execute("DELETE FROM spec_notes WHERE bike_id IS NULL AND field_key=?", (key,))
        log_action(ctx, "field.note", f'Cleared the site-wide note on "{field["label"]}"', target=key)
        ctx.conn.commit()
        return {"ok": True, "note": None}
    if len(body) > NOTE_MAX:
        raise HttpError(400, f"a note is limited to {NOTE_MAX} characters")
    # NULL never conflicts in a UNIQUE, so the one site-wide note is replaced by hand
    had = ctx.conn.execute("SELECT 1 FROM spec_notes WHERE bike_id IS NULL AND field_key=?", (key,)).fetchone()
    if had:
        ctx.conn.execute(
            "UPDATE spec_notes SET body=?, written_by=?, updated_at=datetime('now')"
            " WHERE bike_id IS NULL AND field_key=?", (body, ctx.user["id"], key))
    else:
        ctx.conn.execute(
            "INSERT INTO spec_notes (bike_id, field_key, body, written_by) VALUES (NULL,?,?,?)",
            (key, body, ctx.user["id"]))
    on = ctx.conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
    log_action(ctx, "field.note", f'Note on "{field["label"]}", shown on {on} bike(s)',
               target=key, detail={"body": body, "bikes": on})
    ctx.conn.commit()
    return {"ok": True, "note": _note_of(ctx.conn, None, key), "bikes": on}


def _note_of(conn, bike_id, key):
    if bike_id is None:
        row = one(conn.execute(
            "SELECT n.body, n.created_at, n.updated_at, u.username AS written_by"
            " FROM spec_notes n LEFT JOIN users u ON u.id = n.written_by"
            " WHERE n.bike_id IS NULL AND n.field_key=?", (key,)))
    else:
        row = one(conn.execute(
            "SELECT n.body, n.created_at, n.updated_at, u.username AS written_by"
            " FROM spec_notes n LEFT JOIN users u ON u.id = n.written_by"
            " WHERE n.bike_id=? AND n.field_key=?", (bike_id, key)))
    return row


# ===========================================================================
# MAIL
# ===========================================================================
def send_email(to, subject, body):
    """Hand one message to Resend. Returns nothing: nobody waits on it.

    Mail is slow and fails in ways that have nothing to do with the request
    that triggered it, so it goes out on its own thread. A reset endpoint that
    blocked for fifteen seconds on a DNS timeout would be worse than one whose
    mail is a second late, and an endpoint that 500s because the mail provider
    is down would tell an attacker which addresses exist.
    """
    if not MAIL_KEY:
        print(f"[mail] no RESEND_API_KEY set, so nothing was sent\n"
              f"  to:      {to}\n  subject: {subject}\n  ---\n"
              + "\n".join("  " + ln for ln in body.splitlines()), flush=True)
        return

    def deliver():
        payload = json.dumps({"from": MAIL_FROM, "to": [to],
                              "subject": subject, "text": body}).encode("utf-8")
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload, method="POST",
            headers={"Authorization": f"Bearer {MAIL_KEY}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                r.read()
        except Exception as e:                      # noqa: BLE001 - logged, never raised
            print(f"[mail] FAILED to={to} subject={subject!r}: {e}", flush=True)

    threading.Thread(target=deliver, daemon=True).start()


# ===========================================================================
# FORGOTTEN PASSWORDS
# ===========================================================================
def _hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@route("POST", r"/api/auth/forgot")
def request_password_reset(ctx):
    """Send a reset link, if that address belongs to an account.

    The answer is the same either way. Saying "no account with that address"
    would turn this form into a way to find out who has an account here, which
    is somebody's business and not a stranger's.
    """
    if not MAIL_KEY:
        raise HttpError(503, "password reset by email is not switched on yet — "
                             "ask the site admin to set your password")
    email = (ctx.body.get("email") or "").strip().lower()
    same = {"ok": True, "sent": True}
    if not email or not EMAIL_RE.match(email):
        return same
    user = one(ctx.conn.execute(
        "SELECT id, username, email, suspended FROM users"
        " WHERE email = ? COLLATE NOCASE", (email,)))
    # A suspended account is not a way back in either, and its owner does not
    # need a link they cannot use.
    if not user or user["suspended"]:
        return same

    # One live link at a time. A second request spends the first, so a link
    # forwarded or left in an inbox stops working the moment another is asked
    # for -- and a mailbox full of valid links never accumulates.
    ctx.conn.execute(
        "UPDATE password_resets SET used_at = datetime('now')"
        " WHERE user_id = ? AND used_at IS NULL", (user["id"],))
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=RESET_HOURS)
    ctx.conn.execute(
        "INSERT INTO password_resets (user_id, token_hash, expires_at) VALUES (?,?,?)",
        (user["id"], _hash_token(token), expires.strftime("%Y-%m-%d %H:%M:%S")))
    ctx.conn.commit()

    link = f"{SITE_URL}/reset.html?token={quote(token)}"
    send_email(
        user["email"], "Set a new GearHeadSpecs password",
        f"Somebody asked for a new password for {user['username']} on GearHeadSpecs.\n\n"
        f"Set one here:\n{link}\n\n"
        f"The link works once and stops working in {RESET_HOURS} hour"
        f"{'' if RESET_HOURS == 1 else 's'}.\n\n"
        "If that was not you, nothing has happened to your account and you can\n"
        "ignore this. Your password only changes when somebody uses the link.\n")
    return same


@route("POST", r"/api/auth/reset")
def use_password_reset(ctx):
    """Spend a reset link and set the new password.

    _set_password ends every session for the user, which is the point: if
    somebody else had got into the account, this is what puts them out."""
    token = (ctx.body.get("token") or "").strip()
    password = ctx.body.get("password") or ""
    row = one(ctx.conn.execute(
        "SELECT r.id, r.user_id, r.used_at, r.expires_at, u.username, u.suspended"
        " FROM password_resets r JOIN users u ON u.id = r.user_id"
        " WHERE r.token_hash = ?", (_hash_token(token),))) if token else None
    if not row or row["used_at"] or row["suspended"]:
        raise HttpError(400, "that link has already been used, or is not valid — "
                             "ask for a new one")
    if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc) < datetime.now(timezone.utc):
        raise HttpError(400, f"that link expired — they last {RESET_HOURS} hour"
                             f"{'' if RESET_HOURS == 1 else 's'}. Ask for a new one")
    _set_password(ctx.conn, row["user_id"], password)
    ctx.conn.execute("UPDATE password_resets SET used_at = datetime('now')"
                     " WHERE id = ?", (row["id"],))
    ctx.conn.commit()
    return {"ok": True, "username": row["username"]}


# ===========================================================================
# ENRICHMENT — tools and links on a (bike, field)
# ===========================================================================
@route("GET", r"/api/bikes/(\d+)/fields/([a-z0-9_]+)/enrichment")
def get_enrichment(ctx):
    bike_id, field_key = int(ctx.params[0]), ctx.params[1]
    uid = ctx.user["id"] if ctx.user else -1
    is_manager = bool(ctx.user) and manages_bike(ctx.conn, ctx.user, bike_id)

    # A paused entry stays visible to the manager who paused it (they need to
    # unpause it) but is hidden from everyone else.
    pause_clause = "" if is_manager else " AND paused = 0"

    spec = one(ctx.conn.execute(
        "SELECT s.id, s.value, s.tools, s.confidence, f.label, f.category"
        " FROM specs s JOIN spec_fields f ON f.field_key=s.field_key"
        " WHERE s.bike_id=? AND s.field_key=?", (bike_id, field_key)))
    if not spec:
        raise HttpError(404, "no such spec on this bike")

    return {
        "spec": spec,
        "is_manager": is_manager,
        "tools": rows(ctx.conn.execute(
            f"SELECT t.id, t.text, t.paused, u.username AS added_by"
            f" FROM spec_tools t LEFT JOIN users u ON u.id=t.added_by"
            f" WHERE t.bike_id=? AND t.field_key=?{pause_clause}"
            f" ORDER BY t.id", (bike_id, field_key))),
        "links": rows(ctx.conn.execute(
            f"SELECT l.id, l.link_type, l.title, l.url, l.paused, c.votes,"
            f"  u.username AS added_by,"
            f"  EXISTS(SELECT 1 FROM link_votes v WHERE v.link_id=l.id AND v.user_id=?) AS my_vote,"
            f"  EXISTS(SELECT 1 FROM link_flags g WHERE g.link_id=l.id AND g.flagged_by=?) AS my_flag,"
            f"  (SELECT COUNT(*) FROM link_flags g WHERE g.link_id=l.id AND g.status='open') AS flags"
            f" FROM spec_links l"
            f" JOIN link_vote_counts c ON c.link_id=l.id"
            f" LEFT JOIN users u ON u.id=l.added_by"
            f" WHERE l.bike_id=? AND l.field_key=?{pause_clause}"
            f" ORDER BY c.votes DESC, l.id", (uid, uid, bike_id, field_key))),
    }


@route("POST", r"/api/bikes/(\d+)/fields/([a-z0-9_]+)/tools", role="user")
def add_tool(ctx):
    bike_id, field_key = int(ctx.params[0]), ctx.params[1]
    try:
        cur = ctx.conn.execute(
            "INSERT INTO spec_tools (bike_id, field_key, text, added_by)"
            " VALUES (?,?,?,?)",
            (bike_id, field_key, ctx.field("text"), ctx.user["id"]))
    except sqlite3.IntegrityError:
        raise HttpError(409, "that tool is already listed")
    ctx.conn.commit()
    return {"id": cur.lastrowid}


@route("POST", r"/api/bikes/(\d+)/fields/([a-z0-9_]+)/links", role="user")
def add_link(ctx):
    bike_id, field_key = int(ctx.params[0]), ctx.params[1]
    link_type = ctx.field("link_type")
    if link_type not in ("yt", "forum", "doc", "other"):
        raise HttpError(400, "unknown link type")
    url = ctx.field("url")
    if not re.match(r"^https?://", url):
        raise HttpError(400, "link must start with http:// or https://")
    try:
        cur = ctx.conn.execute(
            "INSERT INTO spec_links (bike_id, field_key, link_type, title, url,"
            " added_by) VALUES (?,?,?,?,?,?)",
            (bike_id, field_key, link_type, ctx.field("title"), url,
             ctx.user["id"]))
    except sqlite3.IntegrityError:
        raise HttpError(409, "that link is already listed")
    ctx.conn.commit()
    return {"id": cur.lastrowid}


@route("POST", r"/api/links/(\d+)/vote", role="user")
def toggle_link_vote(ctx):
    link_id = int(ctx.params[0])
    if not ctx.conn.execute("SELECT 1 FROM spec_links WHERE id=?", (link_id,)).fetchone():
        raise HttpError(404, "link not found")
    if ctx.conn.execute("SELECT 1 FROM link_votes WHERE link_id=? AND user_id=?",
                        (link_id, ctx.user["id"])).fetchone():
        ctx.conn.execute("DELETE FROM link_votes WHERE link_id=? AND user_id=?",
                         (link_id, ctx.user["id"]))
        voted = False
    else:
        ctx.conn.execute("INSERT INTO link_votes (link_id, user_id) VALUES (?,?)",
                         (link_id, ctx.user["id"]))
        voted = True
    ctx.conn.commit()
    votes = ctx.conn.execute(
        "SELECT votes FROM link_vote_counts WHERE link_id=?", (link_id,)).fetchone()[0]
    return {"voted": voted, "votes": votes}


@route("POST", r"/api/links/(\d+)/flag", role="user")
def flag_link(ctx):
    link_id = int(ctx.params[0])
    detail = ctx.field("detail", required=False)
    if detail and len(detail) > 150:
        raise HttpError(400, "detail is limited to 150 characters")
    try:
        ctx.conn.execute(
            "INSERT INTO link_flags (link_id, flagged_by, reason, detail)"
            " VALUES (?,?,?,?)",
            (link_id, ctx.user["id"], ctx.field("reason"), detail))
    except sqlite3.IntegrityError:
        raise HttpError(409, "you have already flagged this link")
    ctx.conn.commit()
    return {"ok": True}


@route("DELETE", r"/api/links/(\d+)/flag", role="user")
def unflag_link(ctx):
    """The 'flagged by mistake' control."""
    ctx.conn.execute("DELETE FROM link_flags WHERE link_id=? AND flagged_by=?",
                     (int(ctx.params[0]), ctx.user["id"]))
    ctx.conn.commit()
    return {"ok": True}


@route("POST", r"/api/(tools|links)/(\d+)/pause", role="manager")
def toggle_pause(ctx):
    kind, item_id = ctx.params[0], int(ctx.params[1])
    table = "spec_tools" if kind == "tools" else "spec_links"
    row = one(ctx.conn.execute(f"SELECT bike_id, paused FROM {table} WHERE id=?", (item_id,)))
    if not row:
        raise HttpError(404, "not found")
    require_manages(ctx.conn, ctx.user, row["bike_id"])
    new = 0 if row["paused"] else 1
    ctx.conn.execute(f"UPDATE {table} SET paused=? WHERE id=?", (new, item_id))
    ctx.conn.commit()
    return {"paused": bool(new)}


# ===========================================================================
# GARAGE + SERVICE LOG
# ===========================================================================
def _interval_miles(conn, bike_id, task):
    """Read the interval from the spec sheet, falling back to the task default.

    Reading it from the spec is the point: correct the oil-change interval on
    the spec page and every rider's next-due date moves with it, instead of the
    task table quietly holding a stale copy.
    """
    if task["interval_field_key"]:
        row = conn.execute(
            "SELECT value FROM specs WHERE bike_id=? AND field_key=?",
            (bike_id, task["interval_field_key"])).fetchone()
        if row and row[0]:
            m = re.search(r"([\d,]+)\s*mi\b", row[0], re.I)
            if m:
                return int(m.group(1).replace(",", ""))
    return task["default_interval_miles"]


@route("GET", r"/api/garage", role="user")
def get_garage(ctx):
    return {"garage": rows(ctx.conn.execute(
        "SELECT ub.id, ub.nickname, ub.mileage, y.year, y.id AS bike_year_id,"
        " d.bike_id, d.display_name"
        " FROM user_bikes ub"
        " JOIN bike_years y ON y.id = ub.bike_year_id"
        " JOIN bike_display d ON d.bike_id = y.bike_id"
        " WHERE ub.user_id=? ORDER BY ub.id", (ctx.user["id"],)))}


@route("POST", r"/api/garage", role="user")
def add_to_garage(ctx):
    bike_year_id = ctx.field("bike_year_id")
    if not ctx.conn.execute("SELECT 1 FROM bike_years WHERE id=?", (bike_year_id,)).fetchone():
        raise HttpError(404, "no such model year")
    cur = ctx.conn.execute(
        "INSERT INTO user_bikes (user_id, bike_year_id, nickname, mileage)"
        " VALUES (?,?,?,?)",
        (ctx.user["id"], bike_year_id, ctx.body.get("nickname"),
         ctx.body.get("mileage")))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


def _own_garage_row(ctx, ub_id):
    row = one(ctx.conn.execute(
        "SELECT ub.*, y.bike_id FROM user_bikes ub"
        " JOIN bike_years y ON y.id=ub.bike_year_id WHERE ub.id=?", (ub_id,)))
    if not row:
        raise HttpError(404, "not in your garage")
    if row["user_id"] != ctx.user["id"]:
        raise HttpError(403, "not your bike")
    return row


@route("PATCH", r"/api/garage/(\d+)", role="user")
def update_garage(ctx):
    ub = _own_garage_row(ctx, int(ctx.params[0]))
    ctx.conn.execute(
        "UPDATE user_bikes SET nickname=COALESCE(?,nickname),"
        " mileage=COALESCE(?,mileage) WHERE id=?",
        (ctx.body.get("nickname"), ctx.body.get("mileage"), ub["id"]))
    ctx.conn.commit()
    return {"ok": True}


@route("DELETE", r"/api/garage/(\d+)", role="user")
def remove_garage(ctx):
    ub = _own_garage_row(ctx, int(ctx.params[0]))
    ctx.conn.execute("DELETE FROM user_bikes WHERE id=?", (ub["id"],))
    ctx.conn.commit()
    return {"ok": True}


@route("GET", r"/api/garage/(\d+)/service", role="user")
def garage_service(ctx):
    ub = _own_garage_row(ctx, int(ctx.params[0]))
    tasks = rows(ctx.conn.execute(
        "SELECT * FROM service_tasks ORDER BY sort_order"))
    out = []
    for t in tasks:
        linked = rows(ctx.conn.execute(
            "SELECT f.label, s.value, s.confidence, s.tools"
            " FROM service_task_specs ts"
            " JOIN spec_fields f ON f.field_key = ts.field_key"
            " LEFT JOIN specs s ON s.field_key = ts.field_key AND s.bike_id = ?"
            " WHERE ts.task_key = ? ORDER BY ts.sort_order",
            (ub["bike_id"], t["task_key"])))
        # A task whose specs do not exist on this bike is not a task for this
        # bike — a shaft-drive machine has no chain to lube.
        if not any(l["value"] is not None for l in linked):
            continue
        history = rows(ctx.conn.execute(
            "SELECT id, performed_on, miles, note FROM service_log"
            " WHERE user_bike_id=? AND task_key=?"
            " ORDER BY performed_on DESC, id DESC",
            (ub["id"], t["task_key"])))
        interval = _interval_miles(ctx.conn, ub["bike_id"], t)
        status, due_in = "never", None
        if history:
            last = history[0]
            if interval and last["miles"] is not None and ub["mileage"] is not None:
                due_in = (last["miles"] + interval) - ub["mileage"]
                status = "overdue" if due_in < 0 else ("soon" if due_in < interval * 0.15 else "ok")
            else:
                status = "logged"
        out.append({**t, "specs": linked, "history": history,
                    "interval_miles": interval, "status": status,
                    "due_in_miles": due_in})
    return {"bike": ub, "tasks": out}


@route("POST", r"/api/garage/(\d+)/service", role="user")
def log_service(ctx):
    ub = _own_garage_row(ctx, int(ctx.params[0]))
    task_key = ctx.field("task_key")
    if not ctx.conn.execute("SELECT 1 FROM service_tasks WHERE task_key=?",
                            (task_key,)).fetchone():
        raise HttpError(404, "unknown task")
    performed_on = ctx.field("performed_on")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", performed_on):
        raise HttpError(400, "performed_on must be YYYY-MM-DD")
    miles = ctx.body.get("miles")
    cur = ctx.conn.execute(
        "INSERT INTO service_log (user_bike_id, task_key, performed_on, miles, note)"
        " VALUES (?,?,?,?,?)",
        (ub["id"], task_key, performed_on, miles, ctx.body.get("note")))
    # Logging service at a higher odometer reading is the most reliable moment
    # to learn the bike's current mileage, so take it.
    if miles and (ub["mileage"] is None or miles > ub["mileage"]):
        ctx.conn.execute("UPDATE user_bikes SET mileage=? WHERE id=?", (miles, ub["id"]))
    ctx.conn.commit()
    return {"id": cur.lastrowid}


# ===========================================================================
# QUESTIONNAIRE
#
# Two distinct jobs, easy to confuse:
#   /definition + /build  — decide WHICH fields this bike has (the Spec Tree)
#   /<id> + /answer       — fill in the values for fields it already has
# ===========================================================================
@route("GET", r"/api/questionnaire/definition", role="manager")
def questionnaire_definition(ctx):
    doc = questionnaire.load()
    return {"questions": doc["questions"], "primary_order": doc["primary_order"],
            "bike_types": doc["bike_types"]}


@route("POST", r"/api/bikes", role="admin")
def create_bike(ctx):
    """Create a bike, then run the questionnaire against it to give it a tree.

    Admin only. A manager does not pick up bikes — an admin assigns them, so
    letting a manager create one would be self-assignment through the back door.
    """
    make = ctx.field("make")
    model_code = ctx.field("model_code")
    year_start = int(ctx.field("year_start"))
    year_end = int(ctx.body.get("year_end") or year_start)
    bike_id, universal = _create_bike(
        ctx.conn, make, model_code, year_start, year_end,
        ctx.body.get("bike_type"), ctx.body.get("name"))
    # A new bike starts with no manager. Assigning one is a separate, deliberate
    # admin action — see /api/admin/bikes/<id>/manager.
    ctx.conn.commit()
    return {"bike_id": bike_id, "universal_fields": universal}


def _create_bike(conn, make, model_code, year_start, year_end, bike_type=None, name=None):
    """The bike row, its primary name, its model years and its universal
    fields, uncommitted. Shared by admin's "add a bike" and by turning a
    rider's request into a bike, so the two cannot drift apart."""
    if year_end < year_start:
        raise HttpError(400, "year_end cannot be before year_start")
    try:
        cur = conn.execute(
            "INSERT INTO bikes (make, model_code, year_start, year_end,"
            " bike_type, years_verified) VALUES (?,?,?,?,?,1)",
            (make, model_code, year_start, year_end, bike_type))
    except sqlite3.IntegrityError:
        raise HttpError(409, "a bike with that make, model and start year exists")
    bike_id = cur.lastrowid

    conn.execute(
        "INSERT INTO bike_names (bike_id, name, market, is_primary)"
        " VALUES (?,?,'',1)",
        (bike_id, name or f"{make} {model_code}"))
    for y in range(year_start, year_end + 1):
        try:
            conn.execute(
                "INSERT INTO bike_years (bike_id, year, market) VALUES (?,?,'')",
                (bike_id, y))
        except sqlite3.IntegrityError:
            # The overlap trigger fired: another bike of this model already
            # owns that year. Roll the whole thing back rather than leave a
            # bike covering a partial range it did not ask for.
            conn.rollback()
            raise HttpError(409, f"model year {y} is already claimed by another bike")

    # Fields marked universal belong on every bike, including one created a
    # year after the field was. Applied here so a bike is never missing them
    # just because nobody re-ran a back-fill.
    universal = conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " SELECT ?, field_key, NULL, 'pending' FROM spec_fields WHERE universal=1",
        (bike_id,)).rowcount
    return bike_id, universal


@route("POST", r"/api/questionnaire/(\d+)/build", role="manager")
def build_spec_tree(ctx):
    """Turn questionnaire answers into the bike's set of fields.

    The bike's manager, or an admin. Answering "does this bike have a battery?"
    is knowledge about one machine, which is exactly what the manager was
    assigned for — it is not a platform decision. What stays with admin is
    which fields EXIST at all: a branch proposal creates a field for every bike
    in the catalogue, and that is a different kind of authority from saying
    which existing questions apply to yours.

    The server re-walks the branching rather than trusting a field list from
    the client — otherwise the tree would be whatever the browser claimed.
    """
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    answers = ctx.body.get("answers")
    if not isinstance(answers, dict) or not answers:
        raise HttpError(400, "answers must be an object of question -> option")

    try:
        path, fields, not_sure, bike_type, answers = questionnaire.run(answers)
    except questionnaire.QuestionnaireError as e:
        raise HttpError(400, str(e))
    # From here on `answers` is only what the walk used. The submitted payload
    # can carry replies to questions this bike never reached — the wizard keeps
    # them so going back shows what you chose — and acting on those would put
    # specs on a bike for a branch it is not on.

    created = 0
    for label, _category in fields.items():
        key = re.sub(r"[^a-z0-9]+", "_",
                     label.lower().replace("×", " x ").replace("&", " and ")).strip("_")
        if not ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                                (key,)).fetchone():
            # Should be unreachable: the seeder registers every triggerable
            # field. If it happens, the questionnaire and the registry have
            # drifted, and silently inventing a field would hide that.
            raise HttpError(500, f"questionnaire triggered unregistered field {label!r}")
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
            " VALUES (?,?,NULL,'pending')", (bike_id, key))
        created += cur.rowcount

    # Universal fields, which no answer controls.
    cur = ctx.conn.execute(
        "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
        " SELECT ?, field_key, NULL, 'pending' FROM spec_fields WHERE universal=1",
        (bike_id,))
    created += cur.rowcount
    universal_added = cur.rowcount

    # Fields attached to a branch after the fact, by an approved proposal.
    # questionnaire.json only knows the built-in tree; without this pass an
    # approved field would never reach a bike no matter how it answered.
    extra = 0
    for qid, opt in answers.items():
        for row in ctx.conn.execute(
                "SELECT field_key FROM field_triggers"
                " WHERE question_id=? AND option_label=?", (qid, opt)):
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                " VALUES (?,?,NULL,'pending')", (bike_id, row[0]))
            created += cur.rowcount
            extra += cur.rowcount

    # Keep the answers. They are the only record of why this bike has these
    # fields, and a field attached to a branch later needs them to find the
    # bikes it should apply to. Re-running replaces rather than accumulates.
    ctx.conn.execute("DELETE FROM bike_answers WHERE bike_id=?", (bike_id,))
    ctx.conn.executemany(
        "INSERT INTO bike_answers (bike_id, question_id, option_label, answered_by)"
        " VALUES (?,?,?,?)",
        [(bike_id, qid, opt, ctx.user["id"]) for qid, opt in answers.items()])

    for item in not_sure:
        ctx.conn.execute(
            "INSERT INTO not_sure_answers (bike_id, question_text, submitted_by)"
            " VALUES (?,?,?)", (bike_id, item["text"], ctx.user["id"]))

    if bike_type:
        ctx.conn.execute("UPDATE bikes SET bike_type=? WHERE id=?",
                         (bike_type, bike_id))

    # Re-answering only ever ADDS. If a bike previously answered "has a battery"
    # and now says it does not, the battery fields stay — removing them would
    # destroy values somebody sourced, on a re-run that might have been a
    # mis-click. So report them instead: the manager can see what no longer
    # fits and have it removed deliberately.
    triggered = set()
    for label in fields:
        triggered.add(re.sub(r"[^a-z0-9]+", "_",
                             label.lower().replace("×", " x ").replace("&", " and ")
                             ).strip("_"))
    for qid, opt in answers.items():
        for row in ctx.conn.execute(
                "SELECT field_key FROM field_triggers"
                " WHERE question_id=? AND option_label=?", (qid, opt)):
            triggered.add(row[0])
    for row in ctx.conn.execute(
            "SELECT field_key FROM spec_fields WHERE universal=1"):
        triggered.add(row[0])

    stale = rows(ctx.conn.execute(
        "SELECT f.label, s.value FROM specs s"
        " JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE s.bike_id = ?", (bike_id,)))
    present = rows(ctx.conn.execute(
        "SELECT field_key FROM specs WHERE bike_id = ?", (bike_id,)))
    no_longer = [r["field_key"] for r in present if r["field_key"] not in triggered]
    no_longer_detail = rows(ctx.conn.execute(
        "SELECT f.label, s.value IS NOT NULL AND s.value <> '' AS has_value"
        " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
        " WHERE s.bike_id = ? AND s.field_key IN (%s)"
        % (",".join("?" * len(no_longer)) or "''"),
        [bike_id] + no_longer)) if no_longer else []

    ctx.conn.commit()
    return {"questions_asked": len(path),
            "no_longer_applies": no_longer_detail,
            "fields_triggered": len(fields) + extra + universal_added,
            "from_approved_branches": extra,
            "universal_fields": universal_added,
            "specs_created": created, "not_sure": len(not_sure),
            "bike_type": bike_type}


@route("GET", r"/api/questionnaire/(\d+)", role="manager")
def questionnaire_gaps(ctx):
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    return {
        "bike": one(ctx.conn.execute(
            "SELECT * FROM bike_display WHERE bike_id=?", (bike_id,))),
        "questions": rows(ctx.conn.execute(
            "SELECT s.id AS spec_id, s.field_key, f.label, f.category,"
            " f.spec_type, s.value, s.confidence"
            " FROM specs s JOIN spec_fields f ON f.field_key=s.field_key"
            " WHERE s.bike_id=? AND (s.value IS NULL OR s.value='')"
            " ORDER BY f.sort_order, f.label", (bike_id,))),
        "answered": ctx.conn.execute(
            "SELECT COUNT(*) FROM specs WHERE bike_id=? AND value IS NOT NULL"
            " AND value <> ''", (bike_id,)).fetchone()[0],
        # What this bike answered last time, so re-running the questionnaire
        # starts from those answers instead of a blank form.
        "answers": {r["question_id"]: r["option_label"] for r in rows(ctx.conn.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?",
            (bike_id,)))},
    }


@route("POST", r"/api/questionnaire/(\d+)/answer", role="manager")
def answer_question(ctx):
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    field_key = ctx.field("field_key")
    spec = one(ctx.conn.execute(
        "SELECT s.id, f.label FROM specs s JOIN spec_fields f ON f.field_key=s.field_key"
        " WHERE s.bike_id=? AND s.field_key=?", (bike_id, field_key)))
    if not spec:
        raise HttpError(404, "no such field on this bike")

    if ctx.body.get("not_sure"):
        # Not an answer — a question for admin. The spec stays NULL, which is
        # honest: still a gap, now with someone looking at it.
        cur = ctx.conn.execute(
            "INSERT INTO not_sure_answers (bike_id, field_key, question_text,"
            " submitted_by) VALUES (?,?,?,?)",
            (bike_id, field_key, spec["label"], ctx.user["id"]))
        ctx.conn.commit()
        return {"not_sure_id": cur.lastrowid}

    confidence = ctx.body.get("confidence") or "pending"
    if confidence not in ("confirmed", "mfr", "pending"):
        raise HttpError(400, "unknown confidence")
    ctx.conn.execute(
        "UPDATE specs SET value=?, confidence=?, value_source=NULL, entered_by=?,"
        " updated_at=datetime('now') WHERE id=?",
        (ctx.field("value"), confidence, ctx.user["id"], spec["id"]))
    ctx.conn.commit()
    return {"ok": True}


# ===========================================================================
# ADMIN
# ===========================================================================
@route("GET", r"/api/admin/summary", role="admin")
def admin_summary(ctx):
    c = ctx.conn
    q = lambda sql: c.execute(sql).fetchone()[0]
    return {
        "tree_flags":      q("SELECT COUNT(*) FROM tree_flags WHERE status='open'"),
        "proposals":       q("SELECT COUNT(*) FROM branch_proposals WHERE status='pending'"),
        "user_flags":      q("SELECT COUNT(*) FROM user_flags WHERE status='open'"),
        "not_sure":        q("SELECT COUNT(*) FROM not_sure_answers WHERE status='pending'"),
        "divergence":      q("SELECT COUNT(*) FROM divergence_queue"),
        "link_flags":      q("SELECT COUNT(*) FROM link_flags WHERE status='open'"),
        "unverified_years": q("SELECT COUNT(*) FROM bikes WHERE years_verified=0"),
        "manager_changes": q("SELECT COUNT(*) FROM manager_notices WHERE seen_at IS NULL"),
        "field_requests":  q("SELECT COUNT(*) FROM (SELECT 1 FROM field_requests"
                             " WHERE status='pending' GROUP BY bike_id, field_key)"),
        "bike_requests":   q("SELECT COUNT(*) FROM bike_requests WHERE status='pending'"),
        "managers":        q("SELECT COUNT(DISTINCT user_id) FROM bike_managers"),
        "managed_bikes":   q("SELECT COUNT(DISTINCT bike_id) FROM bike_managers"),
        "catalog_dropped": q("SELECT COUNT(*) FROM catalog_v2_dropped"),
        "bikes":           q("SELECT COUNT(*) FROM bikes"),
        "specs":           q("SELECT COUNT(*) FROM specs"),
        "users":           q("SELECT COUNT(*) FROM users"),
    }


@route("GET", r"/api/admin/tree-flags", role="admin")
def admin_tree_flags(ctx):
    return {"items": rows(ctx.conn.execute(
        "SELECT t.id, t.question_text, t.comment, t.status, t.created_at,"
        " u.username AS flagged_by, u.id AS flagged_by_id,"
        " t.bike_id, d.display_name AS bike_name, d.year_range"
        " FROM tree_flags t LEFT JOIN users u ON u.id=t.flagged_by"
        " LEFT JOIN bike_display d ON d.bike_id=t.bike_id"
        " WHERE t.status='open' ORDER BY t.created_at DESC"))}


@route("POST", r"/api/admin/tree-flags/(\d+)/decide", role="admin")
def decide_tree_flag(ctx):
    status = ctx.field("status")
    if status not in ("accepted", "rejected"):
        raise HttpError(400, "status must be accepted or rejected")
    ctx.conn.execute(
        "UPDATE tree_flags SET status=?, admin_note=?, resolved_at=datetime('now')"
        " WHERE id=? AND status='open'",
        (status, ctx.body.get("admin_note"), int(ctx.params[0])))
    log_action(ctx, "treeflag.decide", f"{status.title()} tree flag #{ctx.params[0]}",
               target=ctx.params[0], detail={"status": status,
                                             "note": ctx.body.get("admin_note")})
    ctx.conn.commit()
    return {"ok": True}


@route("GET", r"/api/admin/proposals", role="admin")
def admin_proposals(ctx):
    return {"items": rows(ctx.conn.execute(
        # bike_id, not just bike_name: the console offers "add this field to the
        # proposing bike now", and that needs the id to send back.
        "SELECT p.id, p.field_name, p.category, p.reasoning, p.status, p.value_type,"
        " p.created_at, p.bike_id, u.username AS proposed_by, u.id AS proposed_by_id,"
        " d.display_name AS bike_name, d.year_range"
        " FROM branch_proposals p LEFT JOIN users u ON u.id=p.proposed_by"
        " LEFT JOIN bike_display d ON d.bike_id=p.bike_id"
        " WHERE p.status='pending' ORDER BY p.created_at DESC"))}


def parse_triggers(ctx):
    """Read a branch list off the request.

    Accepts the list form `triggers: [{question, option}, ...]` and the older
    single `trigger_question`/`trigger_option` pair, so existing callers keep
    working while the dialog gains a second branch.

    Several branches on one field are a UNION, not a contradiction: a chain/belt
    tension spec legitimately belongs on q3=A and q3=B while staying off q3=C.
    build_spec_tree already ORs them.
    """
    raw = ctx.body.get("triggers")
    if raw is None:
        qid = ctx.body.get("trigger_question")
        opt = ctx.body.get("trigger_option")
        if not qid and not opt:
            return []
        if not (qid and opt):
            raise HttpError(400, "a branch needs both a question and an option")
        raw = [{"question": qid, "option": opt}]
    if not isinstance(raw, list):
        raise HttpError(400, "triggers must be a list")

    doc = questionnaire.load()
    out = []
    for t in raw:
        qid, opt = (t or {}).get("question"), (t or {}).get("option")
        if not (qid and opt):
            raise HttpError(400, "a branch needs both a question and an option")
        q = doc["questions"].get(qid)
        if not q:
            raise HttpError(400, "no such question: " + str(qid))
        if not any(o["l"] == opt for o in q["options"]):
            raise HttpError(400, repr(opt) + " is not an option for " + str(qid))
        if (qid, opt) not in out:
            out.append((qid, opt))
    return out


def bikes_matching(conn, triggers):
    """Distinct bikes answering ANY of these branches — the back-fill target."""
    if not triggers:
        return []
    clauses = " OR ".join(["(question_id=? AND option_label=?)"] * len(triggers))
    args = [v for pair in triggers for v in pair]
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT bike_id FROM bike_answers WHERE " + clauses, args)]


@route("GET", r"/api/admin/unplaced-fields", role="admin")
def admin_unplaced_fields(ctx):
    """Fields that exist on the Spec Tree but appear on no bike.

    Approving a proposal creates the field; putting it somewhere is a separate
    decision, and it is easy to approve without making it — especially when the
    proposal named no bike and the chosen branch happens to match nothing. The
    result is a field nobody can see, with nothing to say so. This is that
    missing feedback.
    """
    # Fields the static questionnaire can trigger are NOT orphans — they are
    # waiting for a bike that answers that way (belt drive, 2-stroke, carbs 2-6).
    # Lumping them in buries the handful that genuinely reach nothing.
    tree_keys = {
        re.sub(r"[^a-z0-9]+", "_",
               label.lower().replace("×", " x ").replace("&", " and ")).strip("_")
        for label in questionnaire.all_triggerable_fields()
    }

    all_unplaced = rows(ctx.conn.execute(
        "SELECT f.field_key, f.label, f.category, f.spec_type, f.universal, f.created_at,"
        "       p.id AS proposal_id, u.username AS proposed_by,"
        "       (SELECT COUNT(*) FROM field_triggers t"
        "         WHERE t.field_key = f.field_key) AS triggers,"
        "       (SELECT GROUP_CONCAT(t.question_id || '=' || t.option_label, ', ')"
        "          FROM field_triggers t WHERE t.field_key = f.field_key) AS trigger_list"
        " FROM spec_fields f"
        " LEFT JOIN branch_proposals p ON p.created_field_key = f.field_key"
        " LEFT JOIN users u ON u.id = p.proposed_by"
        " WHERE NOT EXISTS (SELECT 1 FROM specs s WHERE s.field_key = f.field_key)"
        " ORDER BY f.created_at DESC"))

    orphans, waiting = [], []
    for f in all_unplaced:
        f["in_questionnaire"] = f["field_key"] in tree_keys
        (waiting if f["in_questionnaire"] else orphans).append(f)

    # orphans   — reach no bike and nothing will ever give them one
    # waiting   — a questionnaire branch will supply them when a bike matches
    return {"fields": orphans, "waiting": waiting,
            "waiting_count": len(waiting)}


# Mirrors seed.CATEGORY_ORDER. The browse page groups by consecutive runs of
# sort_order, so a category's fields have to stay contiguous: each category owns
# a 1000-wide band, and its index here is what picks the band.
CATEGORY_ORDER = ["General", "Engine", "Drive", "Fuel and Air",
                  "Controls", "Suspension", "Electrical", "Gear and Accessories"]
BAND = 1000


def next_sort_order(conn, category):
    """Where a field joining `category` should sit: last among its new siblings.

    Anchored to the category's band rather than to MAX+n alone, because MAX of
    an empty category is nothing — filing the first field of an empty category
    at 1 would drop it into General's band and split General in two on the
    browse page.

    If the band is full the category is compacted back to 10, 20, 30 first.
    That renumbers rows but changes no order, which is the whole point of
    numbering sparsely in the first place.
    """
    if category not in CATEGORY_ORDER:
        raise HttpError(400, f"unknown category: {category!r}")
    base = CATEGORY_ORDER.index(category) * BAND
    top = conn.execute(
        "SELECT MAX(sort_order) FROM spec_fields WHERE category=?",
        (category,)).fetchone()[0]
    if top is None:
        return base + 10
    if top + 10 < base + BAND:
        return top + 10

    siblings = [r[0] for r in conn.execute(
        "SELECT field_key FROM spec_fields WHERE category=?"
        " ORDER BY sort_order, label", (category,))]
    for i, key in enumerate(siblings):
        conn.execute("UPDATE spec_fields SET sort_order=? WHERE field_key=?",
                     (base + (i + 1) * 10, key))
    return base + (len(siblings) + 1) * 10


def log_action(ctx, action, summary, target=None, detail=None, destructive=False):
    """Record an admin action.

    Written on the same connection as the change itself, so it commits or rolls
    back with it — an audit row for something that did not happen would be
    worse than no row at all.

    `detail` carries what a summary cannot: on a deletion, the values that were
    destroyed. That does not make it undoable, but it is the difference between
    "we can find out what was lost" and "it is gone".
    """
    ctx.conn.execute(
        "INSERT INTO admin_actions (user_id, action, target, summary, detail,"
        " destructive) VALUES (?,?,?,?,?,?)",
        (ctx.user["id"], action, str(target) if target is not None else None,
         summary, json.dumps(detail) if detail is not None else None,
         1 if destructive else 0))


@route("GET", r"/api/admin/activity", role="admin")
def admin_activity(ctx):
    """What has been done, most recent first.

    Defaults to the signed-in admin's own actions, since "changes I have made"
    is the usual question; `?who=all` widens it once there is more than one
    admin.
    """
    who = ctx.arg("who", "me")
    limit = min(int(ctx.arg("limit", "60")), 300)
    where, args = "", []
    if who != "all":
        where = " WHERE a.user_id = ?"
        args = [ctx.user["id"]]
    if ctx.arg("destructive") == "1":
        where = (where + " AND" if where else " WHERE") + " a.destructive = 1"

    return {"actions": rows(ctx.conn.execute(
        "SELECT a.id, a.action, a.target, a.summary, a.detail, a.destructive,"
        "       a.created_at, u.username, u.id AS user_id"
        " FROM admin_actions a LEFT JOIN users u ON u.id = a.user_id"
        f"{where} ORDER BY a.created_at DESC, a.id DESC LIMIT ?", args + [limit])),
        "total": ctx.conn.execute(
            f"SELECT COUNT(*) FROM admin_actions a{where}", args).fetchone()[0],
        "destructive_total": ctx.conn.execute(
            "SELECT COUNT(*) FROM admin_actions WHERE destructive = 1").fetchone()[0]}


@route("GET", r"/api/spec-tree", role="manager")
def spec_tree(ctx):
    """The whole tree: every field, what puts it on a bike, how far it is filled.

    Manager and above. Not because the field names are secret — a rider sees
    them on any spec sheet — but because this is the shape of the platform's
    data model, including the fields that reach nobody, and it is a working
    view for the people who maintain it rather than a page for readers.

    One query for the fields and one for the triggers, joined in Python: a
    group_concat over field_triggers would otherwise multiply the spec counts
    for any field sitting on two branches.

    With ?bike=<id> -- a bike the caller manages, or any bike for admin --
    every field also says where it stands ON THAT BIKE: the spec rows it has
    there, whether they are online, whether they hold a value, and the
    manager's per-bike choices (type override, extra headings). That is what
    lets a manager work the tree for their own machine the way admin works it
    for all of them: add a field, take it offline, take it off, without ever
    touching another bike.
    """
    fields = rows(ctx.conn.execute(
        "SELECT f.field_key, f.label, f.category, f.spec_type, f.universal, f.value_type,"
        "       f.sort_order,"
        # Admin's site-wide headings, so a per-bike view can show them as
        # fixed rather than as something the manager could untick.
        "       (SELECT GROUP_CONCAT(g.category, '|') FROM spec_field_categories g"
        "         WHERE g.field_key = f.field_key AND g.shown = 1) AS also_in_site,"
        "       EXISTS(SELECT 1 FROM spec_field_categories g"
        "         WHERE g.field_key = f.field_key AND g.shown = 0) AS home_hidden_site,"
        "       (SELECT COUNT(*) FROM specs s WHERE s.field_key=f.field_key)"
        "         AS bikes,"
        "       (SELECT COUNT(*) FROM specs s WHERE s.field_key=f.field_key"
        "          AND s.value IS NOT NULL AND TRIM(s.value)<>'') AS filled,"
        # How many of those rows no rider can see. A field staged offline reads
        # as "on 262 bikes" without this, which is true and misleading.
        "       (SELECT COUNT(*) FROM specs s WHERE s.field_key=f.field_key"
        "          AND s.paused=1) AS offline"
        " FROM spec_fields f ORDER BY f.sort_order, f.label"))

    triggers = {}
    for t in rows(ctx.conn.execute(
            "SELECT field_key, question_id, option_label FROM field_triggers"
            " ORDER BY field_key, question_id, option_label")):
        triggers.setdefault(t["field_key"], []).append(t)

    # The option's own wording, so the page can say "Belt drive" rather than
    # making the reader decode q3=B.
    defn = questionnaire.load()["questions"]
    for f in fields:
        out = []
        for t in triggers.get(f["field_key"], []):
            q = defn.get(t["question_id"], {})
            text = next((o["t"] for o in q.get("options", [])
                         if o["l"] == t["option_label"]), t["option_label"])
            out.append({"question_id": t["question_id"],
                        "option": t["option_label"], "text": text,
                        "question": q.get("text", "")})
        f["triggers"] = out
        f["also_in_site"] = [c for c in (f.pop("also_in_site") or "").split("|") if c]
        f["home_hidden_site"] = bool(f["home_hidden_site"])

    # The caller's own bikes, for the page to offer. Admin manages every
    # bike, so for them the page searches the catalog instead of listing it.
    my_bikes = [] if ctx.user["role"] == "admin" else rows(ctx.conn.execute(
        "SELECT d.bike_id, d.display_name, d.year_range"
        " FROM bike_managers m JOIN bike_display d ON d.bike_id = m.bike_id"
        " WHERE m.user_id=? ORDER BY d.display_name", (ctx.user["id"],)))

    bike = None
    if ctx.arg("bike"):
        if not ctx.arg("bike").isdigit():
            raise HttpError(400, "bike must be a bike id")
        bike_id = int(ctx.arg("bike"))
        bike = one(ctx.conn.execute(
            "SELECT bike_id, display_name, year_range, bike_type FROM bike_display"
            " WHERE bike_id=?", (bike_id,)))
        if not bike:
            raise HttpError(404, "no such bike")
        require_manages(ctx.conn, ctx.user, bike_id)
        here = {}
        for r in rows(ctx.conn.execute(
                "SELECT s.id, s.field_key, s.paused, s.spec_type, s.year_from, s.year_to,"
                "  (s.value IS NOT NULL AND TRIM(s.value)<>'') AS has_value,"
                "  (SELECT COUNT(*) FROM spec_alternates a WHERE a.spec_id = s.id) AS alternates"
                " FROM specs s WHERE s.bike_id=? ORDER BY s.field_key, s.year_from", (bike_id,))):
            h = here.setdefault(r["field_key"], {
                "spec_ids": [], "rows": 0, "online": 0, "values": 0, "alternates": 0,
                "spec_type": None, "also_in": [], "home_hidden": False})
            h["spec_ids"].append(r["id"])
            h["rows"] += 1
            h["online"] += 0 if r["paused"] else 1
            h["values"] += 1 if r["has_value"] else 0
            h["alternates"] += r["alternates"]
            # one override per field on a bike in practice; the first row's
            # stands for the year variants, which the sheet edits together
            if h["spec_type"] is None:
                h["spec_type"] = r["spec_type"]
        for r in rows(ctx.conn.execute(
                "SELECT field_key, category, shown FROM bike_spec_categories WHERE bike_id=?",
                (bike_id,))):
            if r["field_key"] not in here:
                continue
            if r["shown"]:
                here[r["field_key"]]["also_in"].append(r["category"])
            else:
                here[r["field_key"]]["home_hidden"] = True
        for f in fields:
            f["here"] = here.get(f["field_key"])

    return {
        "fields": fields,
        "categories": [c[0] for c in ctx.conn.execute(
            "SELECT category FROM spec_fields"
            " GROUP BY category ORDER BY MIN(sort_order)")],
        "bikes_total": ctx.conn.execute(
            "SELECT COUNT(*) FROM bikes").fetchone()[0],
        "my_bikes": my_bikes,
        "bike": bike,
    }


@route("GET", r"/api/admin/fields", role="admin")
def admin_list_fields(ctx):
    """Every field on the Spec Tree, with what removing it would cost.

    values_set is the number that carry a value somebody sourced. Deleting a
    field cascades its spec rows away, taking those values, their alternates and
    their votes with them — so the count has to be on screen before the button
    is, not in a dialog after.
    """
    q = ctx.arg("q", "").strip()
    where, args = "", []
    if q:
        where = " WHERE f.label LIKE ? OR f.category LIKE ?"
        args = [f"%{q}%", f"%{q}%"]
    limit = min(int(ctx.arg("limit", "40")), 200)

    fields = rows(ctx.conn.execute(
        "SELECT f.field_key, f.label, f.category, f.spec_type, f.universal, f.value_type,"
        "       f.sort_order, f.example,"
        # Where it sits in its category, so the ends can hide their arrow.
        "       (SELECT COUNT(*) FROM spec_fields x WHERE x.category=f.category"
        "          AND x.sort_order < f.sort_order) AS position,"
        "       (SELECT COUNT(*) FROM spec_fields x WHERE x.category=f.category) AS siblings,"
        "       (SELECT COUNT(*) FROM specs s WHERE s.field_key=f.field_key) AS bikes,"
        "       (SELECT COUNT(*) FROM specs s WHERE s.field_key=f.field_key"
        "          AND s.value IS NOT NULL AND s.value<>'') AS values_set,"
        "       (SELECT COUNT(*) FROM field_triggers t WHERE t.field_key=f.field_key) AS triggers,"
        "       (SELECT GROUP_CONCAT(t.question_id || '=' || t.option_label, ', ')"
        "          FROM field_triggers t WHERE t.field_key=f.field_key) AS trigger_list"
        f" FROM spec_fields f{where}"
        # By sort_order, not label: the rows carry "3 of 40" and up/down arrows,
        # and listing them alphabetically would show position 3 above position 1.
        " ORDER BY f.sort_order LIMIT ?", args + [limit]))

    # "q3=A" is precise and unreadable. Resolve every branch to the question
    # as asked and the answer as offered — and carry the other options too, so
    # the console can show the whole question rather than one line of it.
    defn = questionnaire.load()["questions"]
    trig = {}
    for t in rows(ctx.conn.execute(
            "SELECT field_key, question_id, option_label FROM field_triggers"
            " ORDER BY field_key, question_id, option_label")):
        q = defn.get(t["question_id"], {})
        opts = q.get("options", [])
        trig.setdefault(t["field_key"], []).append({
            "question_id": t["question_id"],
            "option": t["option_label"],
            "question": q.get("text", ""),
            "section": q.get("section", ""),
            "answer": next((o["t"] for o in opts if o["l"] == t["option_label"]),
                           t["option_label"]),
            "options": [{"l": o["l"], "t": o["t"]} for o in opts],
        })
    site, hidden = {}, set()
    for r in rows(ctx.conn.execute(
            "SELECT field_key, category, shown FROM spec_field_categories")):
        if r["shown"]:
            site.setdefault(r["field_key"], []).append(r["category"])
        else:
            hidden.add(r["field_key"])
    offline_on = {}
    for r in ctx.conn.execute("SELECT field_key, bike_type FROM field_offline_defaults ORDER BY bike_type"):
        offline_on.setdefault(r[0], []).append(r[1])
    for f in fields:
        f["trigger_details"] = trig.get(f["field_key"], [])
        f["also_in"] = sorted(site.get(f["field_key"], []),
                              key=lambda c: CATEGORY_ORDER.index(c) if c in CATEGORY_ORDER else 99)
        f["home_shown"] = f["field_key"] not in hidden
        f["offline_on"] = offline_on.get(f["field_key"], [])
        n = _note_of(ctx.conn, None, f["field_key"])
        f["note"] = n["body"] if n else None

    return {"fields": fields, "bike_types": questionnaire.load()["bike_types"],
            "total": ctx.conn.execute(
                f"SELECT COUNT(*) FROM spec_fields f{where}", args).fetchone()[0]}


@route("POST", r"/api/admin/fields", role="admin")
def admin_create_field(ctx):
    """Add a spec to the tree directly.

    Until now the only way to create a field was to approve a branch proposal,
    which meant an admin who wanted one had to file it as a manager first and
    then approve their own request. The proposal flow exists so a manager can
    ASK; it should not be the only door for the person who decides.

    Placement is part of creation, not an afterthought. A field with no branch,
    no universal flag and no named bike exists on the tree and reaches nobody —
    that is what the "Fields on no bike" panel is full of — so this refuses to
    create one unless it is told where it goes.
    """
    label = ctx.field("label")
    if len(label) > 120:
        raise HttpError(400, "field name is limited to 120 characters")

    key = re.sub(r"[^a-z0-9]+", "_",
                 label.lower().replace("×", " x ").replace("&", " and ")).strip("_")
    if not key:
        raise HttpError(400, "field name must contain letters or numbers")
    if ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                        (key,)).fetchone():
        raise HttpError(409, f"a field named {label!r} already exists")

    category = ctx.field("category")
    if category not in CATEGORY_ORDER:
        raise HttpError(400, "category must be one of: " + ", ".join(CATEGORY_ORDER))

    spec_type = ctx.body.get("spec_type", "pref")
    if spec_type not in ("fixed", "pref", "community"):
        raise HttpError(400, "spec_type must be fixed, pref or community")
    value_type = value_type_from(ctx.body)

    triggers = parse_triggers(ctx)
    universal = bool(ctx.body.get("universal"))
    bike_ids = ctx.body.get("bike_ids") or []
    # Staged: the field is on the tree and attached to its bikes, but no rider
    # sees it until it is switched on. For a spec being written up, or one whose
    # branch you want to check before it lands on 262 spec sheets.
    offline = bool(ctx.body.get("offline"))

    if universal and triggers:
        raise HttpError(409, "a field cannot be on every bike and restricted to"
                             " a branch at the same time — universal wins"
                             " silently, which is how a 2-stroke oil spec ended"
                             " up on four-strokes")
    if not (universal or triggers or bike_ids):
        raise HttpError(400, "say where it goes: a branch, every bike, or"
                             " named bikes. A field with none of those reaches"
                             " nobody.")

    order = next_sort_order(ctx.conn, category)
    ctx.conn.execute(
        "INSERT INTO spec_fields (field_key, label, category, spec_type,"
        " sort_order, universal, value_type) VALUES (?,?,?,?,?,?,?)",
        (key, label, category, spec_type, order, 1 if universal else 0, value_type))

    for qid, opt in triggers:
        ctx.conn.execute(
            "INSERT OR IGNORE INTO field_triggers"
            " (field_key, question_id, option_label, created_by)"
            " VALUES (?,?,?,?)", (key, qid, opt, ctx.user["id"]))

    # paused_by/at are set on the same INSERT rather than a second UPDATE, so a
    # staged row is never briefly visible between the two.
    pz, pb = (1, ctx.user["id"]) if offline else (0, None)
    pa = "datetime('now')" if offline else "NULL"

    added = 0
    if universal:
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence,"
            f" paused, paused_by, paused_at)"
            f" SELECT id, ?, NULL, 'pending', ?, ?, {pa} FROM bikes", (key, pz, pb))
        added += cur.rowcount
    if triggers and ctx.body.get("backfill"):
        for bike in bikes_matching(ctx.conn, triggers):
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value,"
                " confidence, paused, paused_by, paused_at)"
                f" VALUES (?,?,NULL,'pending',?,?,{pa})", (bike, key, pz, pb))
            added += cur.rowcount
    for bike in bike_ids:
        if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike,)).fetchone():
            raise HttpError(404, f"no bike with id {bike}")
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence,"
            " paused, paused_by, paused_at)"
            f" VALUES (?,?,NULL,'pending',?,?,{pa})", (bike, key, pz, pb))
        added += cur.rowcount

    where = ("every bike" if universal else
             ", ".join(f"{q}={o}" for q, o in triggers) if triggers else
             f"{len(bike_ids)} named bike(s)")
    log_action(ctx, "field.create",
               f'Added "{label}" to {category} on {where}'
               f" ({added} spec row(s)"
               + (", offline" if offline else "") + ")",
               target=key,
               detail={"label": label, "category": category,
                       "spec_type": spec_type, "universal": universal,
                       "offline": offline,
                       "triggers": [{"question": q, "option": o} for q, o in triggers],
                       "bike_ids": bike_ids, "rows_added": added})
    ctx.conn.commit()
    return {"ok": True, "field_key": key, "label": label, "category": category,
            "universal": universal, "rows_added": added, "offline": offline,
            "triggers": [{"question": q, "option": o} for q, o in triggers]}


@route("GET", r"/api/admin/fields/([a-z0-9_]+)/usage", role="admin")
def admin_field_usage(ctx):
    """Which bikes carry this field, and which of them have a value."""
    field_key = ctx.params[0]
    if not ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                            (field_key,)).fetchone():
        raise HttpError(404, "no such field")
    return {"bikes": rows(ctx.conn.execute(
        "SELECT s.id AS spec_id, s.bike_id, d.display_name, d.year_range,"
        "       s.value, s.confidence,"
        "       (SELECT COUNT(*) FROM spec_alternates a WHERE a.spec_id=s.id) AS alternates"
        " FROM specs s JOIN bike_display d ON d.bike_id = s.bike_id"
        " WHERE s.field_key = ?"
        " ORDER BY (s.value IS NULL OR s.value=''), d.display_name LIMIT 200",
        (field_key,)))}


@route("DELETE", r"/api/admin/bikes/(\d+)/specs/([a-z0-9_]+)", role="admin")
def admin_remove_spec_from_bike(ctx):
    """Take one field off one bike.

    Refuses while the spec still holds a value unless told explicitly: removing
    it deletes that value and any alternates and votes hanging off it, and
    "this field does not belong on this bike" is a different statement from
    "throw away what people recorded".
    """
    bike_id, field_key = int(ctx.params[0]), ctx.params[1]
    spec = one(ctx.conn.execute(
        "SELECT id, value FROM specs WHERE bike_id=? AND field_key=?",
        (bike_id, field_key)))
    if not spec:
        raise HttpError(404, "that bike does not have this field")

    has_value = spec["value"] not in (None, "")
    alternates = ctx.conn.execute(
        "SELECT COUNT(*) FROM spec_alternates WHERE spec_id=?", (spec["id"],)).fetchone()[0]
    if (has_value or alternates) and not ctx.body.get("force"):
        raise HttpError(409, "this spec holds a value or alternates — "
                             "removing it destroys them; confirm to proceed")

    meta = one(ctx.conn.execute(
        "SELECT d.display_name, f.label FROM bike_display d, spec_fields f"
        " WHERE d.bike_id = ? AND f.field_key = ?", (bike_id, field_key)))
    ctx.conn.execute("DELETE FROM specs WHERE id=?", (spec["id"],))
    forget_placements(ctx.conn, bike_id, field_key)
    log_action(ctx, "spec.remove",
               f'Removed "{meta["label"]}" from {meta["display_name"]}'
               + (f' — destroyed the value "{spec["value"]}"' if has_value else ""),
               target=field_key, destructive=bool(has_value or alternates),
               detail={"bike_id": bike_id, "bike": meta["display_name"],
                       "label": meta["label"], "value": spec["value"],
                       "alternates_destroyed": alternates})
    ctx.conn.commit()
    return {"ok": True, "value_destroyed": has_value, "alternates_destroyed": alternates}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/type", role="admin")
def admin_set_field_type(ctx):
    """Change a field's type across the whole tree.

    Distinct from PATCH /api/specs/<id>/type, which is a bike manager marking
    one spec on their own bike. This one is the platform-wide default for the
    field, which is admin's to decide — the same split as everywhere else:
    admin owns the tree, the manager owns their bike.

    'fixed' refuses alternates everywhere the field appears, so turning it on
    reports how many bikes carry it and how many alternates already exist that
    would be contradicted.
    """
    field_key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT field_key, label, spec_type FROM spec_fields WHERE field_key=?",
        (field_key,)))
    if not field:
        raise HttpError(404, "no such field")

    spec_type = ctx.field("spec_type")
    if spec_type not in ("fixed", "pref", "community"):
        raise HttpError(400, "spec_type must be fixed, pref or community")

    # Existing alternates are not deleted — they are somebody's contribution,
    # and marking the field fixed is a statement about future ones. But the
    # admin should know they are there.
    orphaned = ctx.conn.execute(
        "SELECT COUNT(*) FROM spec_alternates a JOIN specs s ON s.id = a.spec_id"
        " WHERE s.field_key = ?", (field_key,)).fetchone()[0]

    ctx.conn.execute("UPDATE spec_fields SET spec_type=? WHERE field_key=?",
                     (spec_type, field_key))
    log_action(ctx, "field.type",
               f'"{field["label"]}" is now {spec_type}'
               + (f" (was {field['spec_type']})" if field["spec_type"] != spec_type else ""),
               target=field_key,
               detail={"from": field["spec_type"], "to": spec_type,
                       "existing_alternates": orphaned})
    ctx.conn.commit()
    return {"ok": True, "spec_type": spec_type, "was": field["spec_type"],
            "existing_alternates": orphaned}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/value-type", role="admin")
def admin_set_value_type(ctx):
    """Switch a field between typed text and a closed vocabulary.

    Going TO a vocabulary is only allowed while every value already on the
    field -- stock values and alternates, on every bike -- reads as one of its
    entries; otherwise the page would show a picker beside a value the picker
    cannot express. The ones that do not parse are listed so they can be
    fixed first. Going back to text is always allowed: the canonical forms
    ("yellow/red", "AKI:91") are readable as they are.
    """
    key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT label, value_type FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field")
    new = value_type_from(ctx.body)
    was = field["value_type"]
    if new == was:
        return {"ok": True, "value_type": new, "was": was, "rewritten": 0}

    rewritten = 0
    if new != "text":
        bad = []
        stock = rows(ctx.conn.execute(
            "SELECT s.id, s.value, d.display_name FROM specs s"
            " JOIN bike_display d ON d.bike_id = s.bike_id"
            " WHERE s.field_key=? AND s.value IS NOT NULL AND TRIM(s.value) <> ''", (key,)))
        alts = rows(ctx.conn.execute(
            "SELECT a.id, a.text AS value, d.display_name FROM spec_alternates a"
            " JOIN specs s ON s.id = a.spec_id"
            " JOIN bike_display d ON d.bike_id = s.bike_id"
            " WHERE s.field_key=? AND a.text IS NOT NULL AND TRIM(a.text) <> ''", (key,)))
        fixes = []
        for kind, items in (("specs", stock), ("spec_alternates", alts)):
            for it in items:
                try:
                    canon = normalise_for_type(new, it["value"])
                except (wire_colors.WireColorError, fuel_octane.FuelValueError) as e:
                    bad.append({"bike": it["display_name"], "value": it["value"], "why": str(e)})
                    continue
                if canon != it["value"]:
                    fixes.append((kind, it["id"], canon))
        if bad:
            raise HttpError(409, f"{len(bad)} value(s) on {field['label']} do not read as "
                                 f"{new.replace('_', ' ')} -- fix or clear them first: "
                                 + "; ".join(f"{b['bike']}: {b['value']!r} ({b['why']})"
                                             for b in bad[:8])
                                 + (" ..." if len(bad) > 8 else ""))
        # Values that parsed but were typed loosely ("Yellow / Red") take
        # their canonical form now, so every value on the field is uniform.
        for kind, rid, canon in fixes:
            col = "value" if kind == "specs" else "text"
            ctx.conn.execute(f"UPDATE {kind} SET {col}=? WHERE id=?", (canon, rid))
        rewritten = len(fixes)

    ctx.conn.execute("UPDATE spec_fields SET value_type=? WHERE field_key=?", (new, key))
    log_action(ctx, "field.value_type",
               f'"{field["label"]}" values are now {new.replace("_", " ")} (was {was.replace("_", " ")})'
               + (f"; {rewritten} existing value(s) put in canonical form" if rewritten else ""),
               target=key, detail={"was": was, "value_type": new, "rewritten": rewritten})
    ctx.conn.commit()
    return {"ok": True, "value_type": new, "was": was, "rewritten": rewritten}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/offline-defaults", role="admin")
def admin_set_offline_defaults(ctx):
    """Which kinds of bike get this field offline by default -- the whole
    list, replacing what was there. The field stays on the sheet of every
    bike that has it; only new rows on those types start hidden, and the
    manager can put any one of them online. Existing rows are not touched:
    switching a type on does not hide what is already showing, and switching
    it off does not un-hide what a manager chose to hide.
    """
    key = ctx.params[0]
    field = one(ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?", (key,)))
    if not field:
        raise HttpError(404, "no such field")
    types = ctx.body.get("bike_types")
    if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
        raise HttpError(400, "bike_types must be a list of bike types")
    known = questionnaire.load()["bike_types"]
    bad = [t for t in types if t not in known]
    if bad:
        raise HttpError(400, "unknown bike type: " + ", ".join(bad))
    was = [r[0] for r in ctx.conn.execute(
        "SELECT bike_type FROM field_offline_defaults WHERE field_key=?", (key,))]
    ctx.conn.execute("DELETE FROM field_offline_defaults WHERE field_key=?", (key,))
    ctx.conn.executemany(
        "INSERT INTO field_offline_defaults (field_key, bike_type, created_by) VALUES (?,?,?)",
        [(key, t, ctx.user["id"]) for t in sorted(set(types))])
    log_action(ctx, "field.offline_defaults",
               f'"{field["label"]}" starts offline on: ' + (", ".join(sorted(set(types))) or "no bike type")
               + (f" (was: {', '.join(was)})" if was else ""),
               target=key, detail={"bike_types": sorted(set(types)), "was": was})
    ctx.conn.commit()
    return {"ok": True, "bike_types": sorted(set(types))}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/label", role="admin")
def rename_field(ctx):
    """Change what a field is called on every spec sheet.

    Only the label moves. field_key stays as it was, because it is what specs,
    field_triggers and bike_header_specs all point at, and it is what the
    Guides links carry — the schema separates the two precisely so a rename is
    one UPDATE rather than a sweep across three tables and every saved URL. The
    stale key is invisible to readers; "Curb Weight" was renamed to "Dry Weight"
    this way and its rows still say curb_weight.

    Admin-only: the label is what all 261 bikes show, so this is a Spec Tree
    decision, not something scoped to one manager's bikes.
    """
    field_key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT field_key, label, category FROM spec_fields WHERE field_key=?",
        (field_key,)))
    if not field:
        raise HttpError(404, "no such field")

    label = ctx.field("label")
    if len(label) > 120:
        raise HttpError(400, "label must be 120 characters or fewer")
    if label == field["label"]:
        raise HttpError(409, f"\"{label}\" is already its name")

    # Two fields reading the same on a spec sheet is indistinguishable to the
    # reader, whatever their keys say.
    clash = one(ctx.conn.execute(
        "SELECT field_key, category FROM spec_fields"
        " WHERE LOWER(TRIM(label))=LOWER(TRIM(?)) AND field_key<>?",
        (label, field_key)))
    if clash:
        raise HttpError(409, f"\"{label}\" is already the name of another "
                             f"field, in {clash['category']}")

    ctx.conn.execute("UPDATE spec_fields SET label=? WHERE field_key=?",
                     (label, field_key))
    bikes = ctx.conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.rename",
               f"Renamed \"{field['label']}\" to \"{label}\" "
               f"({bikes} bike{'' if bikes == 1 else 's'} affected)",
               target=field_key,
               detail={"was": field["label"], "now": label,
                       "field_key": field_key, "bikes": bikes})
    ctx.conn.commit()
    return {"ok": True, "field_key": field_key, "label": label,
            "was": field["label"], "bikes": bikes}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/category", role="admin")
def set_field_category(ctx):
    """Re-file a field under a different category.

    Admin-only, and deliberately not something a manager can reach: category
    sits on the field, so moving one moves it on every bike at once. A manager
    is scoped to their own bikes, and this is the opposite of scoped.

    The spec rows are untouched — they reference the field, not the category —
    so no value is at risk. What changes is where the field appears on every
    spec sheet, and its sort_order, which has to be re-derived because the old
    number belongs to the old category's band.

    The field lands last in its new category; ▲▼ moves it from there.
    """
    field_key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT field_key, label, category, sort_order FROM spec_fields"
        " WHERE field_key=?", (field_key,)))
    if not field:
        raise HttpError(404, "no such field")

    category = ctx.field("category")
    if category not in CATEGORY_ORDER:
        raise HttpError(400, "category must be one of: "
                             + ", ".join(CATEGORY_ORDER))
    if category == field["category"]:
        raise HttpError(409, f"\"{field['label']}\" is already in {category}")

    order = next_sort_order(ctx.conn, category)
    ctx.conn.execute(
        "UPDATE spec_fields SET category=?, sort_order=? WHERE field_key=?",
        (category, order, field_key))

    bikes = ctx.conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.category",
               f"Moved \"{field['label']}\" from {field['category']} to "
               f"{category} ({bikes} bike{'' if bikes == 1 else 's'} affected)",
               target=field_key,
               detail={"from": field["category"], "to": category,
                       "was_sort_order": field["sort_order"],
                       "now_sort_order": order, "bikes": bikes})
    ctx.conn.commit()
    return {"ok": True, "field_key": field_key, "category": category,
            "sort_order": order, "bikes": bikes}


@route("POST", r"/api/admin/fields/([a-z0-9_]+)/visibility", role="admin")
def set_field_visibility(ctx):
    """Take a whole field online or offline in one go, on every bike carrying it.

    The per-bike control a manager uses is one spec on one machine. This is the
    other end of it: a field staged offline across 262 bikes needs one switch to
    publish, not 262 visits. Admin-only for the same reason the field itself is
    — it lands on every bike at once.

    Only rows already carrying the field are touched. A bike that answers the
    branch later gets a normal, visible row: "offline" describes the rows that
    exist, not a permanent property of the field.
    """
    field_key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT field_key, label FROM spec_fields WHERE field_key=?", (field_key,)))
    if not field:
        raise HttpError(404, "no such field")

    want = ctx.body.get("online")
    if want is None:
        raise HttpError(400, "online is required (true or false)")
    paused = 0 if want else 1

    cur = ctx.conn.execute(
        "UPDATE specs SET paused=?, paused_by=?,"
        "  paused_at = CASE WHEN ?=1 THEN datetime('now') ELSE NULL END,"
        "  updated_at = datetime('now')"
        " WHERE field_key=? AND paused<>?",
        (paused, ctx.user["id"] if paused else None, paused, field_key, paused))
    changed = cur.rowcount

    total = ctx.conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.visibility",
               f'Took "{field["label"]}" {"online" if want else "offline"}'
               f" on {changed} of {total} bike(s)",
               target=field_key,
               detail={"online": bool(want), "changed": changed, "total": total})
    ctx.conn.commit()
    return {"ok": True, "field_key": field_key, "label": field["label"],
            "online": bool(want), "changed": changed, "total": total}


@route("POST", r"/api/admin/fields/([a-z0-9_]+)/move", role="admin")
def admin_move_field(ctx):
    """Move a field up or down within its category.

    Swaps sort_order with its neighbour rather than recomputing the category:
    two UPDATEs, and every other field keeps the number it had, so nothing else
    shifts underneath a second admin working at the same time.

    Movement is confined to the category. Sort order also decides which
    category band a field sits in, and the browse page builds its category
    headings from consecutive runs — a field crossing a band would appear under
    somebody else's heading. Moving between categories is a different operation.
    """
    field_key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT field_key, label, category, sort_order FROM spec_fields"
        " WHERE field_key=?", (field_key,)))
    if not field:
        raise HttpError(404, "no such field")

    direction = ctx.field("direction")
    if direction not in ("up", "down"):
        raise HttpError(400, "direction must be up or down")

    op, order = ("<", "DESC") if direction == "up" else (">", "ASC")
    neighbour = one(ctx.conn.execute(
        f"SELECT field_key, label, sort_order FROM spec_fields"
        f" WHERE category=? AND sort_order {op} ?"
        f" ORDER BY sort_order {order} LIMIT 1",
        (field["category"], field["sort_order"])))
    if not neighbour:
        raise HttpError(409, f"\"{field['label']}\" is already "
                             f"{'first' if direction == 'up' else 'last'} in "
                             f"{field['category']}")

    ctx.conn.execute("UPDATE spec_fields SET sort_order=? WHERE field_key=?",
                     (neighbour["sort_order"], field["field_key"]))
    ctx.conn.execute("UPDATE spec_fields SET sort_order=? WHERE field_key=?",
                     (field["sort_order"], neighbour["field_key"]))
    log_action(ctx, "field.move",
               f"Moved \"{field['label']}\" {direction} in {field['category']}, "
               f"past \"{neighbour['label']}\"",
               target=field_key,
               detail={"direction": direction, "category": field["category"],
                       "swapped_with": neighbour["field_key"]})
    ctx.conn.commit()
    return {"ok": True, "moved": direction, "swapped_with": neighbour["label"],
            "sort_order": neighbour["sort_order"]}


@route("POST", r"/api/admin/fields/([a-z0-9_]+)/universal", role="admin")
def admin_set_field_universal(ctx):
    """Mark a field as belonging on every bike — or stop it doing so.

    Turning it on does not by itself touch existing bikes; back-filling is a
    separate tick, because it writes one spec row per bike and the admin should
    know that before it happens. Turning it off leaves already-created rows
    alone: they may hold values somebody entered, and silently deleting those
    would be the one destructive thing in this whole flow.
    """
    field_key = ctx.params[0]
    if not ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                            (field_key,)).fetchone():
        raise HttpError(404, "no such field")
    universal = ctx.body.get("universal")
    if not isinstance(universal, bool):
        raise HttpError(400, "universal must be true or false")

    # "Every bike" and "only bikes answering q5=A" are contradictory statements
    # about the same field, and universal silently won — which is how a 2-stroke
    # oil spec ended up on 260 four-strokes. One of the two is wrong and only
    # the admin knows which, so refuse rather than quietly pick one.
    if universal:
        branches = rows(ctx.conn.execute(
            "SELECT question_id, option_label FROM field_triggers WHERE field_key=?",
            (field_key,)))
        if branches:
            listed = ", ".join(b["question_id"] + "=" + b["option_label"]
                               for b in branches)
            raise HttpError(409,
                            "this field is restricted to " + listed +
                            ", which contradicts 'every bike'. Remove the branch "
                            "first if it really belongs on every machine.")

    ctx.conn.execute("UPDATE spec_fields SET universal=? WHERE field_key=?",
                     (1 if universal else 0, field_key))
    added = 0
    if universal and ctx.body.get("backfill"):
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
            " SELECT b.id, ?, NULL, 'pending' FROM bikes b", (field_key,))
        added = cur.rowcount
    label = ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.universal",
               (f'"{label}" now applies to every bike'
                + (f" — added to {added}" if added else "")) if universal
               else f'"{label}" no longer applies to every bike (existing bikes keep it)',
               target=field_key, detail={"universal": universal, "added": added})
    ctx.conn.commit()
    return {"ok": True, "universal": universal, "added": added,
            "bikes": ctx.conn.execute("SELECT COUNT(*) FROM bikes").fetchone()[0]}


@route("POST", r"/api/admin/fields/([a-z0-9_]+)/trigger", role="admin")
def admin_set_field_trigger(ctx):
    """Change which questionnaire branch a field belongs to.

    Approval was the only place a branch could be chosen, so picking the wrong
    option — belt conditioner attached to Driveshaft rather than Belt drive —
    left no way to correct it. Replaces rather than adds: a field belonging to
    two contradictory answers to the same question is a mistake, not a feature.
    """
    field_key = ctx.params[0]
    if not ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                            (field_key,)).fetchone():
        raise HttpError(404, "no such field")

    triggers = parse_triggers(ctx)
    if not triggers:
        raise HttpError(400, "give at least one branch, or DELETE to clear them")

    # Replaces the whole set, so the dialog's list is exactly what ends up
    # stored — removing a branch there actually removes it.
    ctx.conn.execute("DELETE FROM field_triggers WHERE field_key=?", (field_key,))
    for qid, opt in triggers:
        ctx.conn.execute(
            "INSERT INTO field_triggers (field_key, question_id, option_label, created_by)"
            " VALUES (?,?,?,?)", (field_key, qid, opt, ctx.user["id"]))

    matched = bikes_matching(ctx.conn, triggers)
    backfilled = 0
    if ctx.body.get("backfill"):
        for bike in matched:
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                " VALUES (?,?,NULL,'pending')", (bike, field_key))
            backfilled += cur.rowcount
    ctx.conn.commit()

    doc = questionnaire.load()
    labels = [next(o["t"] for o in doc["questions"][q]["options"] if o["l"] == lab)
              for q, lab in triggers]
    label = ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.branches",
               f'Set "{label}" to apply on: {", ".join(labels)}',
               target=field_key,
               detail={"triggers": [{"question": q, "option": o} for q, o in triggers],
                       "matching_bikes": len(matched), "backfilled": backfilled})
    ctx.conn.commit()
    return {"ok": True,
            "triggers": [{"question": q, "option": lab} for q, lab in triggers],
            "option_text": ", ".join(labels),
            "matching_bikes": len(matched), "backfilled": backfilled}


@route("DELETE", r"/api/admin/fields/([a-z0-9_]+)/trigger", role="admin")
def admin_clear_field_trigger(ctx):
    cur = ctx.conn.execute("DELETE FROM field_triggers WHERE field_key=?",
                           (ctx.params[0],))
    if cur.rowcount:
        log_action(ctx, "field.branches.clear",
                   f"Detached {ctx.params[0]} from {cur.rowcount} branch(es)"
                   " — bikes that already have it keep it",
                   target=ctx.params[0], detail={"removed": cur.rowcount})
    ctx.conn.commit()
    return {"ok": True, "removed": cur.rowcount}


@route("POST", r"/api/admin/fields/([a-z0-9_]+)/apply", role="admin")
def admin_apply_field(ctx):
    """Put a field onto named bikes. The 'or just pick specific bike' route —
    and the only one that reaches catalog bikes, which answered no
    questionnaire and so match no branch."""
    field_key = ctx.params[0]
    if not ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                            (field_key,)).fetchone():
        raise HttpError(404, "no such field")
    bike_ids = ctx.body.get("bike_ids")
    if not isinstance(bike_ids, list) or not bike_ids:
        raise HttpError(400, "bike_ids must be a non-empty list")

    added, missing = 0, []
    for bid in bike_ids:
        if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bid,)).fetchone():
            missing.append(bid)
            continue
        cur = ctx.conn.execute(
            "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
            " VALUES (?,?,NULL,'pending')", (bid, field_key))
        added += cur.rowcount
    if missing:
        ctx.conn.rollback()
        raise HttpError(404, f"no such bike: {missing}")
    label = ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.apply",
               f'Added "{label}" to {added} bike(s)',
               target=field_key, detail={"bike_ids": bike_ids, "added": added})
    ctx.conn.commit()
    return {"ok": True, "added": added}


@route("DELETE", r"/api/admin/fields/([a-z0-9_]+)", role="admin")
def admin_delete_field(ctx):
    """Remove a field that should not have been created.

    Refused once any bike carries it — deleting then would cascade away real
    spec rows, and possibly values people had entered. Detach it from the bikes
    first, deliberately.
    """
    field_key = ctx.params[0]
    if not ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                            (field_key,)).fetchone():
        raise HttpError(404, "no such field")

    # A field the questionnaire still asks for cannot simply go: the walk would
    # trigger a label with no field behind it, and every bike answering that
    # way would fail. This is not a "are you sure" — force does not override it,
    # because the fix is to edit the questionnaire, not to accept a broken one.
    label = ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    asked_by = questionnaire.questions_triggering(label)
    if asked_by:
        where = ", ".join(
            f"{h['question_id']}={h['option']}" if h["option"] else h["question_id"]
            for h in asked_by)
        raise HttpError(409,
                        f"the questionnaire still asks for {label!r} at {where}"
                        f" — remove it there first, or answering that way will"
                        f" fail for every bike")

    used = ctx.conn.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                            (field_key,)).fetchone()[0]
    with_values = ctx.conn.execute(
        "SELECT COUNT(*) FROM specs WHERE field_key=? AND value IS NOT NULL"
        " AND value <> ''", (field_key,)).fetchone()[0]

    # Refuse by default when in use. Deleting cascades the spec rows, and with
    # them any values, alternates and votes — the counts go back so the console
    # can say exactly what is about to be lost rather than "are you sure?".
    if used and not ctx.body.get("force"):
        raise HttpError(409, f"{used} bike(s) already use this field"
                             f"{f', {with_values} with a value' if with_values else ''}"
                             f" — confirm to delete it and them")

    # Capture what is about to be destroyed, before it is. A summary saying
    # "63 values lost" is useless for recovery; the values themselves are not.
    lost = rows(ctx.conn.execute(
        "SELECT s.bike_id, d.display_name, s.value, s.confidence"
        " FROM specs s JOIN bike_display d ON d.bike_id = s.bike_id"
        " WHERE s.field_key = ? AND s.value IS NOT NULL AND s.value <> ''",
        (field_key,)))
    label = ctx.conn.execute("SELECT label FROM spec_fields WHERE field_key=?",
                             (field_key,)).fetchone()[0]

    ctx.conn.execute("DELETE FROM field_triggers WHERE field_key=?", (field_key,))
    ctx.conn.execute("DELETE FROM spec_fields WHERE field_key=?", (field_key,))
    log_action(ctx, "field.delete",
               f'Deleted the field "{label}"'
               + (f" from {used} bike(s), destroying {with_values} value(s)" if used else ""),
               target=field_key, destructive=bool(used),
               detail={"label": label, "bikes_affected": used,
                       "values_destroyed": with_values, "lost_values": lost})
    ctx.conn.commit()
    return {"ok": True, "bikes_affected": used, "values_destroyed": with_values}


@route("GET", r"/api/admin/branches", role="admin")
def admin_branches(ctx):
    """The questionnaire branches an approved field can be attached to, each
    with how many existing bikes answered that way.

    The count is the blast radius: attaching a field to "Q3 = chain drive" and
    back-filling changes every one of those bikes' spec sheets, and an admin
    should see that number before committing to it.
    """
    doc = questionnaire.load()
    counts = {}
    for r in ctx.conn.execute(
            "SELECT question_id, option_label, COUNT(*) AS n"
            " FROM bike_answers GROUP BY question_id, option_label"):
        counts[f"{r['question_id']}|{r['option_label']}"] = r["n"]

    out = []
    for qid in doc["primary_order"] + [q for q in doc["questions"]
                                       if q not in doc["primary_order"]]:
        q = doc["questions"].get(qid)
        if not q:
            continue
        out.append({
            "question_id": qid,
            "section": q["section"],
            "text": q["text"],
            "options": [{"label": o["l"], "text": o["t"],
                         "bikes": counts.get(f"{qid}|{o['l']}", 0)}
                        for o in q["options"]],
        })
    return {"branches": out,
            "bikes_with_answers": ctx.conn.execute(
                "SELECT COUNT(DISTINCT bike_id) FROM bike_answers").fetchone()[0],
            "bikes_total": ctx.conn.execute(
                "SELECT COUNT(*) FROM bikes").fetchone()[0]}


@route("POST", r"/api/admin/proposals/(\d+)/decide", role="admin")
def decide_proposal(ctx):
    """Approving creates the field for real — that is what makes this an admin
    decision. An approved field becomes available to every bike, not just the
    proposer's, so the tree grows here and nowhere else."""
    pid = int(ctx.params[0])
    status = ctx.field("status")
    if status not in ("approved", "rejected"):
        raise HttpError(400, "status must be approved or rejected")
    p = one(ctx.conn.execute(
        "SELECT * FROM branch_proposals WHERE id=? AND status='pending'", (pid,)))
    if not p:
        raise HttpError(404, "no pending proposal with that id")

    created_key = None
    backfilled = 0
    applied_to_bike = False
    if status == "approved":
        # The admin can reword the field before it is created. A proposer types
        # "belt conditioner"; what goes on the tree is read by every rider on
        # every bike, so the wording is worth settling here rather than living
        # with whatever was typed in a hurry. The proposal keeps its original
        # text either way, so the change stays visible.
        label = (ctx.body.get("field_name") or p["field_name"]).strip()
        if not label:
            raise HttpError(400, "field name cannot be empty")
        if len(label) > 120:
            raise HttpError(400, "field name is limited to 120 characters")

        created_key = re.sub(r"[^a-z0-9]+", "_",
                             label.lower().replace("×", " x ").replace("&", " and ")
                             ).strip("_")
        if not created_key:
            raise HttpError(400, "field name must contain letters or numbers")
        if ctx.conn.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                            (created_key,)).fetchone():
            raise HttpError(409, f"a field named {label!r} already exists")
        # Admin can re-file it on the way in, for the same reason they can
        # reword the name: the proposer picked from a dropdown without seeing
        # how the rest of the tree is organised.
        category = ctx.body.get("category") or p["category"]
        if category not in CATEGORY_ORDER:
            raise HttpError(400, "category must be one of: "
                                 + ", ".join(CATEGORY_ORDER))
        order = next_sort_order(ctx.conn, category)
        value_type = value_type_from(ctx.body) if ctx.body.get("value_type") else p["value_type"]
        ctx.conn.execute(
            "INSERT INTO spec_fields (field_key, label, category, spec_type,"
            " sort_order, from_proposal_id, value_type) VALUES (?,?,?,?,?,?,?)",
            (created_key, label, category,
             ctx.body.get("spec_type", "pref"), order, pid, value_type))

        # Where the field actually lives. Creating the row is not enough: a
        # field with no branch and no bike appears in no tree and does nothing.
        triggers = parse_triggers(ctx)
        for qid, opt in triggers:
            ctx.conn.execute(
                "INSERT OR IGNORE INTO field_triggers"
                " (field_key, question_id, option_label, created_by)"
                " VALUES (?,?,?,?)", (created_key, qid, opt, ctx.user["id"]))

        # Back-filling is the admin's call, which is why the console shows the
        # count first — it rewrites the spec sheet of every matching bike.
        if triggers and ctx.body.get("backfill"):
            for bike in bikes_matching(ctx.conn, triggers):
                cur = ctx.conn.execute(
                    "INSERT OR IGNORE INTO specs"
                    " (bike_id, field_key, value, confidence)"
                    " VALUES (?,?,NULL,'pending')", (bike, created_key))
                backfilled += cur.rowcount

        # Straight onto one named bike — for a one-off, or for a catalog bike
        # that never answered the questionnaire and so no branch can reach.
        if ctx.body.get("apply_to_bike") and p["bike_id"]:
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO specs (bike_id, field_key, value, confidence)"
                " VALUES (?,?,NULL,'pending')", (p["bike_id"], created_key))
            applied_to_bike = bool(cur.rowcount)

    ctx.conn.execute(
        "UPDATE branch_proposals SET status=?, admin_note=?, created_field_key=?,"
        " resolved_at=datetime('now') WHERE id=?",
        (status, ctx.body.get("admin_note"), created_key, pid))

    if status == "approved":
        log_action(ctx, "proposal.approve",
                   f'Approved "{p["field_name"]}"'
                   + (f' as "{label}"' if label != p["field_name"] else "")
                   + f" into {category}"
                   + (f" (proposed for {p['category']})"
                      if category != p["category"] else ""),
                   target=created_key,
                   detail={"proposal_id": pid, "proposed_as": p["field_name"],
                           "created_as": label, "category": category,
                           "proposed_category": p["category"],
                           "triggers": [{"question": q, "option": o} for q, o in triggers],
                           "backfilled": backfilled,
                           "applied_to_proposing_bike": applied_to_bike})
    else:
        log_action(ctx, "proposal.reject", f'Rejected "{p["field_name"]}"',
                   target=str(pid), detail={"proposal_id": pid,
                                            "note": ctx.body.get("admin_note")})
    ctx.conn.commit()
    return {"ok": True, "created_field_key": created_key,
            "created_label": label if status == "approved" else None,
            "renamed": status == "approved" and label != p["field_name"],
            "backfilled": backfilled, "applied_to_bike": applied_to_bike}


@route("GET", r"/api/admin/user-flags", role="admin")
def admin_user_flags(ctx):
    return {"items": rows(ctx.conn.execute(
        "SELECT uf.id, uf.reason, uf.detail, uf.status, uf.created_at,"
        " t.username AS flagged_user, t.id AS flagged_user_id, t.suspended,"
        " b.username AS flagged_by, b.id AS flagged_by_id"
        " FROM user_flags uf JOIN users t ON t.id=uf.flagged_user"
        " JOIN users b ON b.id=uf.flagged_by"
        " WHERE uf.status='open' ORDER BY uf.created_at DESC"))}


@route("POST", r"/api/admin/user-flags/(\d+)/decide", role="admin")
def decide_user_flag(ctx):
    status = ctx.field("status")
    if status not in ("actioned", "dismissed"):
        raise HttpError(400, "status must be actioned or dismissed")
    uf = one(ctx.conn.execute("SELECT * FROM user_flags WHERE id=?",
                              (int(ctx.params[0]),)))
    if not uf:
        raise HttpError(404, "flag not found")
    if status == "actioned" and ctx.body.get("suspend"):
        ctx.conn.execute("UPDATE users SET suspended=1 WHERE id=?", (uf["flagged_user"],))
        # Suspension has to end the sessions too, or the account keeps working
        # until the cookie happens to expire.
        ctx.conn.execute("DELETE FROM sessions WHERE user_id=?", (uf["flagged_user"],))
    ctx.conn.execute(
        "UPDATE user_flags SET status=?, admin_note=?, resolved_at=datetime('now')"
        " WHERE id=?", (status, ctx.body.get("admin_note"), uf["id"]))
    target_name = ctx.conn.execute("SELECT username FROM users WHERE id=?",
                                   (uf["flagged_user"],)).fetchone()[0]
    suspended = status == "actioned" and bool(ctx.body.get("suspend"))
    log_action(ctx, "userflag.decide",
               f"{status.title()} the flag on {target_name}"
               + (" — account suspended and sessions ended" if suspended else ""),
               target=target_name, destructive=suspended,
               detail={"flag_id": uf["id"], "status": status,
                       "suspended": suspended, "reason": uf["reason"]})
    ctx.conn.commit()
    return {"ok": True}


@route("GET", r"/api/admin/not-sure", role="admin")
def admin_not_sure(ctx):
    return {"items": rows(ctx.conn.execute(
        "SELECT n.id, n.question_text, n.field_key, n.status, n.created_at,"
        " u.username AS submitted_by, u.id AS submitted_by_id,"
        " d.display_name AS bike_name, d.year_range, n.bike_id"
        " FROM not_sure_answers n LEFT JOIN users u ON u.id=n.submitted_by"
        " JOIN bike_display d ON d.bike_id=n.bike_id"
        " WHERE n.status='pending' ORDER BY n.created_at DESC"))}


@route("POST", r"/api/admin/not-sure/(\d+)/decide", role="admin")
def decide_not_sure(ctx):
    """Admin confirming an answer writes it to the spec, which is the whole
    point of the queue — otherwise the question is closed and the gap remains."""
    nid = int(ctx.params[0])
    status = ctx.field("status")
    if status not in ("confirmed", "rejected"):
        raise HttpError(400, "status must be confirmed or rejected")
    n = one(ctx.conn.execute(
        "SELECT * FROM not_sure_answers WHERE id=? AND status='pending'", (nid,)))
    if not n:
        raise HttpError(404, "no pending item with that id")
    value = ctx.body.get("value")
    if status == "confirmed":
        if not value:
            raise HttpError(400, "confirming needs a value")
        if not n["field_key"]:
            raise HttpError(400, "this question is not tied to a field, so there "
                                 "is nothing to write a value to")
        ctx.conn.execute(
            "UPDATE specs SET value=?, confidence='confirmed', value_source=NULL, entered_by=?,"
            " updated_at=datetime('now') WHERE bike_id=? AND field_key=?",
            (value, ctx.user["id"], n["bike_id"], n["field_key"]))
    ctx.conn.execute(
        "UPDATE not_sure_answers SET status=?, admin_note=?,"
        " resolved_at=datetime('now') WHERE id=?",
        (status, ctx.body.get("admin_note"), nid))
    log_action(ctx, "notsure.decide",
               (f'Confirmed "{n["question_text"]}" as "{value}"' if status == "confirmed"
                else f'Closed "{n["question_text"]}" without an answer'),
               target=n["field_key"],
               detail={"id": nid, "status": status, "value": value,
                       "bike_id": n["bike_id"]})
    ctx.conn.commit()
    return {"ok": True}


@route("GET", r"/api/admin/divergence", role="admin")
def admin_divergence(ctx):
    return {"items": rows(ctx.conn.execute(
        "SELECT q.*, f.label,"
        " da.display_name AS bike_a_name, db.display_name AS bike_b_name,"
        " da.year_range AS bike_a_years, db.year_range AS bike_b_years"
        " FROM divergence_queue q"
        " JOIN spec_fields f ON f.field_key = q.field_key"
        " JOIN bike_display da ON da.bike_id = q.bike_a"
        " JOIN bike_display db ON db.bike_id = q.bike_b"
        " ORDER BY q.lineage_root, f.label LIMIT 200"))}


@route("POST", r"/api/admin/divergence/ack", role="admin")
def ack_divergence(ctx):
    ctx.conn.execute(
        "INSERT OR IGNORE INTO divergence_ack (bike_a, bike_b, field_key,"
        " value_a, value_b, reviewed_by, note) VALUES (?,?,?,?,?,?,?)",
        (ctx.field("bike_a"), ctx.field("bike_b"), ctx.field("field_key"),
         ctx.body.get("value_a"), ctx.body.get("value_b"),
         ctx.user["id"], ctx.body.get("note")))
    log_action(ctx, "divergence.ack",
               f'Marked {ctx.field("field_key")} reviewed between bikes '
               f'{ctx.field("bike_a")} and {ctx.field("bike_b")}',
               target=ctx.field("field_key"),
               detail={"bike_a": ctx.field("bike_a"), "bike_b": ctx.field("bike_b"),
                       "value_a": ctx.body.get("value_a"),
                       "value_b": ctx.body.get("value_b")})
    ctx.conn.commit()
    return {"ok": True}


@route("GET", r"/api/admin/catalog-dropped", role="admin")
def admin_catalog_dropped(ctx):
    limit = min(int(ctx.arg("limit", "100")), 1000)
    return {"items": rows(ctx.conn.execute(
        "SELECT * FROM catalog_v2_dropped LIMIT ?", (limit,))),
        "total": ctx.conn.execute(
            "SELECT COUNT(*) FROM catalog_v2_dropped").fetchone()[0]}


@route("GET", r"/api/admin/unverified-years", role="admin")
def admin_unverified(ctx):
    return {"items": rows(ctx.conn.execute(
        "SELECT d.bike_id, d.display_name, d.year_range,"
        " (SELECT COUNT(*) FROM bike_years y WHERE y.bike_id=d.bike_id) AS year_count"
        " FROM bike_display d WHERE d.years_verified=0"
        " ORDER BY d.display_name LIMIT 200"))}


# ---------------------------------------------------------------------------
# A bike's identity: its name, and splitting it at a model year.
#
# Both are open to the bike's manager as well as admin. The manager knows the
# machine; making them ask admin to fix "Honda CB919" to "Honda CB900F 919"
# is the kind of gate that stops the fix happening. But both change what every
# rider sees at the top of the page, so when a manager does one the admin is
# told -- a notice after the fact, not a permission.
#
# The rule for when to split, from the owner: split when it is a different
# machine -- different engine, different frame. Year-scope a single spec when
# it is the same machine with a different part. The page says the same.
# ---------------------------------------------------------------------------
def notify_admin(ctx, kind, bike_id, summary, detail=None):
    """A manager changed their own bike. Admin did it themselves -> no notice;
    the audit row is enough."""
    if ctx.user["role"] == "admin":
        return
    ctx.conn.execute(
        "INSERT INTO manager_notices (bike_id, actor, kind, summary, detail)"
        " VALUES (?,?,?,?,?)",
        (bike_id, ctx.user["id"], kind, summary,
         json.dumps(detail) if detail is not None else None))


def _bike_names(conn, bike_id):
    return rows(conn.execute(
        "SELECT name, market, is_primary FROM bike_names WHERE bike_id=?"
        " ORDER BY is_primary DESC, name", (bike_id,)))


@route("PATCH", r"/api/bikes/(\d+)/name", role="manager")
def rename_bike(ctx):
    """Change the name riders see, and the other names the bike is sold under.

    make and model_code are not touched: they are the internal identity the
    catalog keys on and the make+model+year uniqueness rule hangs on. The
    display name is the reader-facing name and moves freely, the same way a
    spec field's label renames while its key stays put.
    """
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    bike = one(ctx.conn.execute(
        "SELECT display_name, year_range FROM bike_display WHERE bike_id=?", (bike_id,)))
    if not bike:
        raise HttpError(404, "bike not found")

    name = ctx.field("name")
    if len(name) > 80:
        raise HttpError(400, "name is too long (80 characters at most)")

    others = ctx.body.get("other_names")
    if others is not None:
        if not isinstance(others, list):
            raise HttpError(400, "other_names must be a list")
        clean = []
        for o in others:
            if not isinstance(o, dict):
                raise HttpError(400, "each other name is {name, market}")
            n = str(o.get("name") or "").strip()
            m = str(o.get("market") or "").strip().upper()
            if not n:
                continue
            if len(n) > 80 or len(m) > 10:
                raise HttpError(400, "an other name or its market is too long")
            clean.append((n, m))
        others = clean

    was = {"display_name": bike["display_name"], "names": _bike_names(ctx.conn, bike_id)}

    # The primary row keeps its market (CB919's is US). A non-primary row that
    # already carries the new name in that market would collide with the
    # UNIQUE (bike_id, name, market); it is the same fact, so it goes.
    primary = one(ctx.conn.execute(
        "SELECT id, market FROM bike_names WHERE bike_id=? AND is_primary=1", (bike_id,)))
    if primary:
        ctx.conn.execute(
            "DELETE FROM bike_names WHERE bike_id=? AND is_primary=0 AND name=? AND market=?",
            (bike_id, name, primary["market"]))
        ctx.conn.execute("UPDATE bike_names SET name=? WHERE id=?", (name, primary["id"]))
        pmarket = primary["market"]
    else:
        ctx.conn.execute(
            "DELETE FROM bike_names WHERE bike_id=? AND name=? AND market=''", (bike_id, name))
        ctx.conn.execute(
            "INSERT INTO bike_names (bike_id, name, market, is_primary) VALUES (?,?,'',1)",
            (bike_id, name))
        pmarket = ""

    if others is not None:
        ctx.conn.execute("DELETE FROM bike_names WHERE bike_id=? AND is_primary=0", (bike_id,))
        for n, m in others:
            if n == name and m == pmarket:
                continue
            ctx.conn.execute(
                "INSERT OR IGNORE INTO bike_names (bike_id, name, market, is_primary)"
                " VALUES (?,?,?,0)", (bike_id, n, m))

    now = _bike_names(ctx.conn, bike_id)
    detail = {"was": was, "now": {"display_name": name, "names": now}}
    changed = was["display_name"] != name
    summary = (f'Renamed "{was["display_name"]}" to "{name}"' if changed
               else f'Updated the other names of "{name}"')
    log_action(ctx, "bike.rename", summary, target=bike_id, detail=detail)
    notify_admin(ctx, "rename", bike_id,
                 f'{ctx.user["username"]} {summary[0].lower() + summary[1:]}'
                 + (f' ({bike["year_range"]})' if bike["year_range"] else ""), detail)
    ctx.conn.commit()
    return {"ok": True, "bike_id": bike_id, "display_name": name, "names": now,
            "notified_admin": ctx.user["role"] != "admin"}


@route("POST", r"/api/bikes/(\d+)/split", role="manager")
def split_bike(ctx):
    """Split a bike at a model year: the years from `at_year` on become a new
    bike with a copy of the spec set.

    What moves and what copies:
      * model years from `at_year` on MOVE to the new bike, row ids intact, so
        a rider's garage entry for an '85 follows the '85.
      * a spec that is year-scoped entirely inside the later span MOVES with
        its alternates, votes and flags; one that straddles the split is cut
        at the year, the earlier part staying and the later part copied.
      * every other spec is COPIED, with its alternates -- the two bikes start
        from one identical sheet, which is what sibling_divergence watches.
        Votes and open flags are not copied: a vote is a rider's opinion about
        one bike, and a flag is a conversation about one row.
      * tools, links, header pins, questionnaire answers and managers are
        copied. The manager who split it keeps both halves.
    """
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    bike = one(ctx.conn.execute("SELECT * FROM bikes WHERE id=?", (bike_id,)))
    if not bike:
        raise HttpError(404, "bike not found")
    try:
        at = int(ctx.field("at_year"))
    except (TypeError, ValueError):
        raise HttpError(400, "at_year must be a year")

    start = bike["year_start"]
    end = bike["year_end"]
    if end is None:  # still current: the last model year on record stands in
        end = ctx.conn.execute("SELECT MAX(year) FROM bike_years WHERE bike_id=?",
                               (bike_id,)).fetchone()[0]
    if start is None or end is None:
        raise HttpError(400, "this bike has no year span to split")
    if not (start < at <= end):
        raise HttpError(400, f"pick a year after {start} and no later than {end}")

    if ctx.conn.execute(
            "SELECT 1 FROM bikes WHERE make=? AND model_code=? AND year_start=?",
            (bike["make"], bike["model_code"], at)).fetchone():
        raise HttpError(409, f"a {bike['make']} {bike['model_code']} already starts at {at}")

    name = one(ctx.conn.execute("SELECT display_name FROM bike_display WHERE bike_id=?",
                                (bike_id,)))["display_name"]
    later_name = (ctx.body.get("name") or "").strip() or None
    if later_name and len(later_name) > 80:
        raise HttpError(400, "name is too long (80 characters at most)")

    c = ctx.conn
    cur = c.execute(
        "INSERT INTO bikes (make, model_code, year_start, year_end, bike_type,"
        " years_verified, split_from_bike_id, split_at_year) VALUES (?,?,?,?,?,1,?,?)",
        (bike["make"], bike["model_code"], at, bike["year_end"], bike["bike_type"],
         bike_id, at))
    new_id = cur.lastrowid
    c.execute("UPDATE bikes SET year_end=?, years_verified=1 WHERE id=?", (at - 1, bike_id))

    moved_years = c.execute(
        "UPDATE bike_years SET bike_id=? WHERE bike_id=? AND year>=?",
        (new_id, bike_id, at)).rowcount

    c.execute("INSERT INTO bike_names (bike_id, name, market, is_primary)"
              " SELECT ?, name, market, is_primary FROM bike_names WHERE bike_id=?",
              (new_id, bike_id))
    if later_name:
        c.execute("DELETE FROM bike_names WHERE bike_id=? AND is_primary=0 AND name=?"
                  " AND market=(SELECT market FROM bike_names WHERE bike_id=? AND is_primary=1)",
                  (new_id, later_name, new_id))
        c.execute("UPDATE bike_names SET name=? WHERE bike_id=? AND is_primary=1",
                  (later_name, new_id))
    c.execute("INSERT INTO bike_managers (user_id, bike_id, specialty)"
              " SELECT user_id, ?, specialty FROM bike_managers WHERE bike_id=?",
              (new_id, bike_id))
    c.execute("INSERT INTO bike_answers (bike_id, question_id, option_label, answered_by, answered_at)"
              " SELECT ?, question_id, option_label, answered_by, answered_at"
              " FROM bike_answers WHERE bike_id=?", (new_id, bike_id))
    c.execute("INSERT INTO spec_tools (bike_id, field_key, text, added_by, paused, created_at)"
              " SELECT ?, field_key, text, added_by, paused, created_at"
              " FROM spec_tools WHERE bike_id=?", (new_id, bike_id))
    c.execute("INSERT INTO spec_links (bike_id, field_key, link_type, title, url, added_by,"
              " paused, created_at) SELECT ?, field_key, link_type, title, url, added_by,"
              " paused, created_at FROM spec_links WHERE bike_id=?", (new_id, bike_id))
    c.execute("INSERT INTO bike_spec_categories (bike_id, field_key, category, shown, placed_by, placed_at)"
              " SELECT ?, field_key, category, shown, placed_by, placed_at"
              " FROM bike_spec_categories WHERE bike_id=?", (new_id, bike_id))
    c.execute("INSERT INTO bike_header_specs (bike_id, field_key, sort_order, hide_below,"
              " hidden, pinned_by, pinned_at) SELECT ?, field_key, sort_order, hide_below,"
              " hidden, pinned_by, pinned_at FROM bike_header_specs WHERE bike_id=?",
              (new_id, bike_id))

    def copy_spec(s, year_from, year_to):
        cur = c.execute(
            "INSERT INTO specs (bike_id, field_key, value, confidence, spec_type, tools,"
            " paused, paused_by, paused_at, entered_by, value_source, year_from, year_to)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id, s["field_key"], s["value"], s["confidence"], s["spec_type"],
             s["tools"], s["paused"], s["paused_by"], s["paused_at"], s["entered_by"],
             s["value_source"], year_from, year_to))
        c.execute(
            "INSERT INTO spec_alternates (spec_id, text, submitted_by, confirmed_fit, paused)"
            " SELECT ?, text, submitted_by, confirmed_fit, paused FROM spec_alternates"
            " WHERE spec_id=?", (cur.lastrowid, s["id"]))

    copied = moved = cut = 0
    for s in rows(c.execute("SELECT * FROM specs WHERE bike_id=? ORDER BY field_key, year_from",
                            (bike_id,))):
        if s["year_from"] is None:
            copy_spec(s, None, None)
            copied += 1
            continue
        lo = s["year_from"]
        hi = s["year_to"] if s["year_to"] is not None else end
        if hi < at:
            continue                                   # earlier years: stays put
        if lo >= at:                                   # later years: moves whole
            c.execute("UPDATE specs SET bike_id=? WHERE id=?", (new_id, s["id"]))
            moved += 1
            continue
        c.execute("UPDATE specs SET year_to=?, updated_at=datetime('now') WHERE id=?",
                  (at - 1, s["id"]))                   # straddles: cut at the year
        copy_spec(s, at, s["year_to"])
        cut += 1

    # A variant left covering its bike's whole span is no longer a variant.
    # "1975-1977" on every spec of a 1975-1977 bike is noise; clearing the
    # range says the same thing the way every other spec does.
    for b_id, b_start, b_end in ((bike_id, start, at - 1), (new_id, at, bike["year_end"])):
        c.execute(
            "UPDATE specs SET year_from=NULL, year_to=NULL WHERE bike_id=?"
            " AND year_from IS NOT NULL AND year_from=?"
            " AND ((? IS NULL AND year_to IS NULL) OR year_to=?)"
            " AND field_key IN (SELECT field_key FROM specs WHERE bike_id=?"
            "                   GROUP BY field_key HAVING COUNT(*)=1)",
            (b_id, b_start, b_end, b_end, b_id))

    earlier = one(c.execute("SELECT bike_id, display_name, year_range FROM bike_display"
                            " WHERE bike_id=?", (bike_id,)))
    later = one(c.execute("SELECT bike_id, display_name, year_range FROM bike_display"
                          " WHERE bike_id=?", (new_id,)))
    detail = {"at_year": at, "earlier": earlier, "later": later,
              "model_years_moved": moved_years, "specs_copied": copied,
              "specs_moved": moved, "specs_cut": cut}
    summary = (f'Split "{name}" at {at}: {earlier["year_range"]} and'
               f' {later["year_range"]}')
    log_action(ctx, "bike.split", summary, target=bike_id, detail=detail)
    notify_admin(ctx, "split", bike_id, f'{ctx.user["username"]} s{summary[1:]}', detail)
    c.commit()
    return {"ok": True, "earlier": earlier, "later": later,
            "model_years_moved": moved_years, "specs_copied": copied,
            "specs_moved": moved, "specs_cut": cut,
            "notified_admin": ctx.user["role"] != "admin"}


# ---------------------------------------------------------------------------
# The bike's photo.
#
# Uploaded by the bike's manager or an admin, as the raw image in the request
# body -- no multipart parsing to get wrong. The type is read from the bytes,
# not the header or the filename: a browser will happily call anything
# image/png, and an SVG is a script with a picture in it. Stored under
# data/photos as <bike_id>.<ext>, served at /photos/<file>.
# ---------------------------------------------------------------------------
PHOTO_DIR = os.path.join(DATA_DIR, "photos") if DATA_DIR else os.path.join(ROOT, "data", "photos")
PHOTO_MAX_BYTES = 5 * 1024 * 1024
RESTORE_MAX_BYTES = 512 * 1024 * 1024
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}


def sniff_image(data):
    """The real type of the bytes, or None for anything not a JPEG, PNG or WebP."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def photo_of(conn, bike_id):
    """The main photo, with the year photos alongside it. None when the
    bike has no main photo -- year photos can still exist then, and the
    page shows them for their years."""
    ps = rows(conn.execute(
        "SELECT p.year, p.file, p.mime, p.bytes, p.uploaded_at, p.uploaded_by AS uploaded_by_id,"
        "       u.username AS uploaded_by"
        " FROM bike_photos p LEFT JOIN users u ON u.id=p.uploaded_by WHERE p.bike_id=?"
        " ORDER BY p.year", (bike_id,)))
    for p in ps:
        # the timestamp in the URL makes a replaced photo show at once instead of
        # the browser's cached copy of the old one
        p["url"] = f"/photos/{p['file']}?v={quote(p['uploaded_at'])}"
    main = next((p for p in ps if p["year"] is None), None)
    years = [p for p in ps if p["year"] is not None]
    return main, years


def _photo_year(ctx, bike_id):
    """The ?year= of a photo request: None for the main photo, else one of
    the bike's model years."""
    raw = ctx.arg("year")
    if raw in (None, ""):
        return None
    if not str(raw).isdigit():
        raise HttpError(400, "year must be a model year")
    year = int(raw)
    if not ctx.conn.execute("SELECT 1 FROM bike_years WHERE bike_id=? AND year=?", (bike_id, year)).fetchone():
        raise HttpError(404, f"{year} is not one of this bike's model years")
    return year


@route("POST", r"/api/bikes/(\d+)/photo", role="manager")
def upload_bike_photo(ctx):
    """A photo for the bike (no ?year=) or for one of its model years.

    The main photo is the first manager's: whoever put it there, or admin,
    can replace or remove it; another manager cannot, and adds a photo for
    the year their own bike is instead. A year photo can be added where
    that year has none, or replaced by whoever added it; a manager holds
    one year photo per bike, so a bike with several managers over the
    years shows each of them once.
    """
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    bike = one(ctx.conn.execute("SELECT display_name, year_range FROM bike_display"
                                " WHERE bike_id=?", (bike_id,)))
    if not bike:
        raise HttpError(404, "bike not found")
    year = _photo_year(ctx, bike_id)
    data = ctx.body.get("_raw") if isinstance(ctx.body, dict) else None
    if not data:
        raise HttpError(400, "send the image itself as the request body,"
                             " with its Content-Type")
    mime = sniff_image(data)
    if not mime:
        raise HttpError(400, "that is not a JPEG, PNG or WebP image")
    if len(data) > PHOTO_MAX_BYTES:
        raise HttpError(413, f"the photo is over {PHOTO_MAX_BYTES // (1024 * 1024)} MB")

    is_admin = ctx.user["role"] == "admin"
    old = one(ctx.conn.execute(
        "SELECT p.file, p.uploaded_by, u.username FROM bike_photos p LEFT JOIN users u ON u.id = p.uploaded_by"
        " WHERE p.bike_id=? AND p.year IS ?", (bike_id, year)))
    if old and old["uploaded_by"] != ctx.user["id"] and not is_admin:
        whose = old["username"] or "an earlier manager"
        if year is None:
            raise HttpError(403, f"the main photo is {whose}'s and stays; add a photo for the year your bike is")
        raise HttpError(403, f"the {year} photo is {whose}'s; pick a year without one")
    if year is not None and not is_admin:
        held = one(ctx.conn.execute(
            "SELECT year FROM bike_photos WHERE bike_id=? AND uploaded_by=? AND year IS NOT NULL AND year<>?",
            (bike_id, ctx.user["id"], year)))
        if held:
            raise HttpError(409, f"you already have the {held['year']} photo on this bike -- one year each;"
                                 " replace or remove that one first")

    os.makedirs(PHOTO_DIR, exist_ok=True)
    fname = f"{bike_id}.{PHOTO_TYPES[mime]}" if year is None else f"{bike_id}-{year}.{PHOTO_TYPES[mime]}"
    with open(os.path.join(PHOTO_DIR, fname), "wb") as f:
        f.write(data)
    if old and old["file"] != fname:              # a PNG replacing a JPEG
        try:
            os.remove(os.path.join(PHOTO_DIR, old["file"]))
        except OSError:
            pass
    # NULL years never conflict in a UNIQUE, so the main photo is updated by
    # hand rather than through ON CONFLICT
    if old:
        ctx.conn.execute(
            "UPDATE bike_photos SET file=?, mime=?, bytes=?, uploaded_by=?, uploaded_at=datetime('now')"
            " WHERE bike_id=? AND year IS ?", (fname, mime, len(data), ctx.user["id"], bike_id, year))
    else:
        ctx.conn.execute(
            "INSERT INTO bike_photos (bike_id, year, file, mime, bytes, uploaded_by, uploaded_at)"
            " VALUES (?,?,?,?,?,?,datetime('now'))",
            (bike_id, year, fname, mime, len(data), ctx.user["id"]))
    main, years = photo_of(ctx.conn, bike_id)
    photo = main if year is None else next(p for p in years if p["year"] == year)
    what = "photo" if year is None else f"{year} photo"
    verb = f"Replaced the {what} of" if old else f"Added a {what} to"
    summary = f'{verb} "{bike["display_name"]}"'
    detail = {"file": fname, "mime": mime, "bytes": len(data), "url": photo["url"],
              "year": year, "replaced": bool(old)}
    log_action(ctx, "bike.photo", summary, target=bike_id, detail=detail)
    notify_admin(ctx, "photo", bike_id,
                 f'{ctx.user["username"]} {summary[0].lower() + summary[1:]}'
                 + (f' ({bike["year_range"]})' if bike["year_range"] else ""), detail)
    ctx.conn.commit()
    return {"ok": True, "photo": photo, "photo_main": main, "year_photos": years,
            "notified_admin": ctx.user["role"] != "admin"}


@route("DELETE", r"/api/bikes/(\d+)/photo", role="manager")
def delete_bike_photo(ctx):
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    year = _photo_year(ctx, bike_id)
    old = one(ctx.conn.execute(
        "SELECT p.id, p.file, p.uploaded_by, u.username FROM bike_photos p LEFT JOIN users u ON u.id = p.uploaded_by"
        " WHERE p.bike_id=? AND p.year IS ?", (bike_id, year)))
    if not old:
        raise HttpError(404, "this bike has no photo" if year is None else f"this bike has no {year} photo")
    if old["uploaded_by"] != ctx.user["id"] and ctx.user["role"] != "admin":
        raise HttpError(403, f"that photo is {old['username'] or 'an earlier manager'}'s")
    ctx.conn.execute("DELETE FROM bike_photos WHERE id=?", (old["id"],))
    try:
        os.remove(os.path.join(PHOTO_DIR, old["file"]))
    except OSError:
        pass
    name = one(ctx.conn.execute("SELECT display_name FROM bike_display WHERE bike_id=?",
                                (bike_id,)))["display_name"]
    log_action(ctx, "bike.photo_remove", f'Removed the {"photo" if year is None else f"{year} photo"} of "{name}"',
               target=bike_id, detail={"file": old["file"], "year": year}, destructive=True)
    ctx.conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# A spec under more than one heading, on one bike.
#
# The field's home category is a Spec Tree fact and admin's to change. This is
# the per-bike addition: "on this bike, also show it under Electrical". The
# page prints a pointer there, so the value, its votes and its flags stay in
# one place and cannot drift.
# ---------------------------------------------------------------------------
@route("POST", r"/api/bikes/(\d+)/categories", role="manager")
def set_spec_categories(ctx):
    bike_id = int(ctx.params[0])
    require_manages(ctx.conn, ctx.user, bike_id)
    field_key = ctx.field("field_key")
    field = one(ctx.conn.execute(
        "SELECT f.label, f.category FROM spec_fields f"
        " WHERE f.field_key=? AND EXISTS (SELECT 1 FROM specs s"
        "   WHERE s.bike_id=? AND s.field_key=f.field_key)", (field_key, bike_id)))
    if not field:
        raise HttpError(404, "this bike has no such spec")
    cats = ctx.body.get("categories")
    if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
        raise HttpError(400, "categories must be a list of category names")
    bad = [c for c in cats if c not in CATEGORY_ORDER]
    if bad:
        raise HttpError(400, f"not a category: {', '.join(bad)}")
    home = field["category"]
    # `categories` is the whole set of headings the manager wants, home
    # included or not. Admin's site-wide decisions are not theirs to undo, so
    # those are folded in regardless of what was sent.
    site_extra = [r["category"] for r in rows(ctx.conn.execute(
        "SELECT category FROM spec_field_categories WHERE field_key=? AND shown=1", (field_key,)))]
    site_hides_home = ctx.conn.execute(
        "SELECT 1 FROM spec_field_categories WHERE field_key=? AND shown=0", (field_key,)).fetchone()
    extras = [c for c in CATEGORY_ORDER if (c in cats or c in site_extra) and c != home]
    home_shown = home in cats and not site_hides_home
    # An empty set is allowed: the spec then shows in the header only -- if it
    # is pinned there, or is a General field -- and under no heading at all.
    # Only the manager's own ticks are stored. A site-wide heading is admin's
    # row, not this bike's; keeping a copy here would outlive admin clearing it.
    own = [c for c in extras if c not in site_extra]

    was = rows(ctx.conn.execute(
        "SELECT category, shown FROM bike_spec_categories WHERE bike_id=? AND field_key=?",
        (bike_id, field_key)))
    ctx.conn.execute("DELETE FROM bike_spec_categories WHERE bike_id=? AND field_key=?",
                     (bike_id, field_key))
    for c in own:
        ctx.conn.execute(
            "INSERT INTO bike_spec_categories (bike_id, field_key, category, shown, placed_by)"
            " VALUES (?,?,?,1,?)", (bike_id, field_key, c, ctx.user["id"]))
    if not home_shown and not site_hides_home:
        ctx.conn.execute(
            "INSERT INTO bike_spec_categories (bike_id, field_key, category, shown, placed_by)"
            " VALUES (?,?,?,0,?)", (bike_id, field_key, home, ctx.user["id"]))
    shown = ([home] if home_shown else []) + extras
    name = one(ctx.conn.execute("SELECT display_name FROM bike_display WHERE bike_id=?",
                                (bike_id,)))["display_name"]
    log_action(ctx, "spec.categories",
               f'"{field["label"]}" on {name} shows under {", ".join(shown) or "no heading (header only)"}',
               target=field_key, detail={"bike_id": bike_id, "was": was, "now": shown})
    ctx.conn.commit()
    return {"ok": True, "field_key": field_key, "home": home, "home_shown": home_shown,
            "shown": shown, "also_in": shown[1:]}


@route("PATCH", r"/api/admin/fields/([a-z0-9_]+)/categories", role="admin")
def set_field_categories(ctx):
    """Show a field under extra headings on every bike.

    Admin-only for the same reason the home category is: it changes every
    spec sheet at once. The home category is not touched; the extra headings
    carry a pointer to the row, not a second copy of it.
    """
    field_key = ctx.params[0]
    field = one(ctx.conn.execute(
        "SELECT label, category FROM spec_fields WHERE field_key=?", (field_key,)))
    if not field:
        raise HttpError(404, "no such field")
    cats = ctx.body.get("categories")
    if not isinstance(cats, list) or not all(isinstance(c, str) for c in cats):
        raise HttpError(400, "categories must be a list of category names")
    bad = [c for c in cats if c not in CATEGORY_ORDER]
    if bad:
        raise HttpError(400, f"not a category: {', '.join(bad)}")
    home = field["category"]
    extras = [c for c in CATEGORY_ORDER if c in cats and c != home]
    home_shown = home in cats
    was = rows(ctx.conn.execute(
        "SELECT category, shown FROM spec_field_categories WHERE field_key=?", (field_key,)))
    ctx.conn.execute("DELETE FROM spec_field_categories WHERE field_key=?", (field_key,))
    for c in extras:
        ctx.conn.execute(
            "INSERT INTO spec_field_categories (field_key, category, shown, placed_by)"
            " VALUES (?,?,1,?)", (field_key, c, ctx.user["id"]))
    if not home_shown:
        ctx.conn.execute(
            "INSERT INTO spec_field_categories (field_key, category, shown, placed_by)"
            " VALUES (?,?,0,?)", (field_key, home, ctx.user["id"]))
    shown = ([home] if home_shown else []) + extras
    bikes = ctx.conn.execute("SELECT COUNT(DISTINCT bike_id) FROM specs WHERE field_key=?",
                             (field_key,)).fetchone()[0]
    log_action(ctx, "field.categories",
               f'"{field["label"]}" shows under {", ".join(shown) or "no heading (header only)"} on every bike',
               target=field_key, detail={"was": was, "now": shown, "bikes": bikes})
    ctx.conn.commit()
    return {"ok": True, "field_key": field_key, "home": home, "home_shown": home_shown,
            "shown": shown, "also_in": extras, "bikes": bikes}


@route("GET", r"/api/admin/notices", role="admin")
def admin_notices(ctx):
    """What managers changed about their bikes, unseen first."""
    return {"items": rows(ctx.conn.execute(
        "SELECT n.id, n.kind, n.summary, n.detail, n.created_at, n.seen_at,"
        " n.bike_id, d.display_name AS bike_name, d.year_range,"
        " u.username AS actor"
        " FROM manager_notices n"
        " LEFT JOIN users u ON u.id = n.actor"
        " LEFT JOIN bike_display d ON d.bike_id = n.bike_id"
        " WHERE n.seen_at IS NULL ORDER BY n.created_at DESC LIMIT 200")),
        "unseen": ctx.conn.execute(
            "SELECT COUNT(*) FROM manager_notices WHERE seen_at IS NULL").fetchone()[0]}


@route("POST", r"/api/admin/notices/seen", role="admin")
def admin_notices_seen(ctx):
    """Mark one notice seen (`id`), or all of them (no id)."""
    nid = ctx.body.get("id")
    if nid is None:
        cur = ctx.conn.execute(
            "UPDATE manager_notices SET seen_by=?, seen_at=datetime('now')"
            " WHERE seen_at IS NULL", (ctx.user["id"],))
    else:
        cur = ctx.conn.execute(
            "UPDATE manager_notices SET seen_by=?, seen_at=datetime('now')"
            " WHERE id=? AND seen_at IS NULL", (ctx.user["id"], int(nid)))
    ctx.conn.commit()
    return {"ok": True, "seen": cur.rowcount}


@route("POST", r"/api/admin/bikes/(\d+)/verify-years", role="admin")
def verify_years(ctx):
    bike_id = int(ctx.params[0])
    name = ctx.conn.execute("SELECT display_name, year_range FROM bike_display"
                            " WHERE bike_id=?", (bike_id,)).fetchone()
    ctx.conn.execute("UPDATE bikes SET years_verified=1 WHERE id=?", (bike_id,))
    log_action(ctx, "bike.verify_years",
               f"Confirmed the year span for {name[0]} ({name[1]})" if name
               else f"Confirmed the year span for bike {bike_id}",
               target=str(bike_id))
    ctx.conn.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Assigning managers to bikes.
#
# Only an admin does this. A manager does not choose which bikes they look
# after, which is what makes "the person closest to the bike" mean something —
# it is a responsibility handed out, not claimed.
# ---------------------------------------------------------------------------
@route("GET", r"/api/admin/assignments", role="admin")
def admin_assignments(ctx):
    return {
        "assignments": rows(ctx.conn.execute(
            "SELECT m.id, m.bike_id, m.specialty, m.created_at,"
            " u.id AS user_id, u.username, u.display_name, u.role,"
            " d.display_name AS bike_name, d.year_range,"
            " p.specs_filled, p.specs_needed,"
            " (SELECT COUNT(*) FROM value_flags vf JOIN specs s ON s.id=vf.spec_id"
            "   WHERE s.bike_id=m.bike_id AND vf.status='open') AS open_flags"
            " FROM bike_managers m"
            " JOIN users u ON u.id = m.user_id"
            " JOIN bike_display d ON d.bike_id = m.bike_id"
            " JOIN bike_spec_progress p ON p.bike_id = m.bike_id"
            " ORDER BY d.display_name")),
        # Candidates for the dropdown: anyone who is not an admin. Admins reach
        # every bike already, so assigning one is a no-op that would just make
        # the list confusing.
        "candidates": rows(ctx.conn.execute(
            "SELECT id, username, display_name, role,"
            " (SELECT COUNT(*) FROM bike_managers m WHERE m.user_id=u.id) AS bikes"
            " FROM users u WHERE role <> 'admin' AND suspended = 0"
            # People who already manage something first — they are the likelier
            # pick — then everyone else, each alphabetical.
            " ORDER BY (role = 'manager') DESC, username")),
        # Bikes with nobody looking after them — the ones actually worth
        # assigning. Capped, since most of the 258-bike catalog is unmanaged.
        # Every unassigned bike, with its make: the console picks a make first
        # and then a model, so a 2,000-row list is never one dropdown. (It was
        # capped at 300 when the catalogue was 264 bikes; the cap hid most of
        # the site once the model lists went in.)
        "unassigned": rows(ctx.conn.execute(
            "SELECT d.bike_id, d.make, d.display_name, d.year_range,"
            " p.fields_triggered, p.specs_filled"
            " FROM bike_display d"
            " JOIN bike_spec_progress p ON p.bike_id = d.bike_id"
            " WHERE NOT EXISTS (SELECT 1 FROM bike_managers m WHERE m.bike_id = d.bike_id)"
            " ORDER BY d.make, d.display_name, d.year_start")),
    }


@route("POST", r"/api/admin/bikes/(\d+)/manager", role="admin")
def assign_manager(ctx):
    bike_id = int(ctx.params[0])
    user_id = int(ctx.field("user_id"))
    if not ctx.conn.execute("SELECT 1 FROM bikes WHERE id=?", (bike_id,)).fetchone():
        raise HttpError(404, "bike not found")
    u = one(ctx.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)))
    if not u:
        raise HttpError(404, "user not found")
    if u["suspended"]:
        raise HttpError(400, "that account is suspended")
    if u["role"] == "admin":
        raise HttpError(400, "admins already reach every bike — "
                             "assigning one would not change anything")
    try:
        ctx.conn.execute(
            "INSERT INTO bike_managers (user_id, bike_id, specialty) VALUES (?,?,?)",
            (user_id, bike_id, ctx.body.get("specialty")))
    except sqlite3.IntegrityError:
        raise HttpError(409, "that person already manages this bike")
    # The first person assigned is the bike's lead manager, and stays named
    # on it after handing it on. Later managers are managers.
    ctx.conn.execute(
        "UPDATE bikes SET lead_manager_id=?, lead_since=datetime('now')"
        " WHERE id=? AND lead_manager_id IS NULL", (user_id, bike_id))

    # Being handed a bike is what makes someone a bike manager. Without this the
    # assignment would exist but every manager route would still 403 them.
    promoted = u["role"] == "user"
    if promoted:
        ctx.conn.execute("UPDATE users SET role='manager' WHERE id=?", (user_id,))
    bike = ctx.conn.execute("SELECT display_name FROM bike_display WHERE bike_id=?",
                            (bike_id,)).fetchone()[0]
    log_action(ctx, "manager.assign",
               f'Assigned {u["username"]} to {bike}'
               + (" (promoted to manager)" if promoted else ""),
               target=str(bike_id),
               detail={"user": u["username"], "bike_id": bike_id,
                       "bike": bike, "promoted": promoted})
    ctx.conn.commit()
    return {"ok": True, "promoted": promoted}


@route("DELETE", r"/api/admin/bikes/(\d+)/manager/(\d+)", role="admin")
def unassign_manager(ctx):
    bike_id, user_id = int(ctx.params[0]), int(ctx.params[1])
    cur = ctx.conn.execute(
        "DELETE FROM bike_managers WHERE bike_id=? AND user_id=?", (bike_id, user_id))
    if not cur.rowcount:
        raise HttpError(404, "that person does not manage this bike")

    # A 'manager' with no bikes has manager access to nothing, so the label
    # would just be wrong. Admins are left alone.
    remaining = ctx.conn.execute(
        "SELECT COUNT(*) FROM bike_managers WHERE user_id=?", (user_id,)).fetchone()[0]
    demoted = False
    if not remaining:
        cur2 = ctx.conn.execute(
            "UPDATE users SET role='user' WHERE id=? AND role='manager'", (user_id,))
        demoted = bool(cur2.rowcount)
    who = ctx.conn.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
    bike = ctx.conn.execute("SELECT display_name FROM bike_display WHERE bike_id=?",
                            (bike_id,)).fetchone()
    log_action(ctx, "manager.unassign",
               f'Removed {who[0] if who else user_id} from '
               f'{bike[0] if bike else bike_id}'
               + (" (set back to user)" if demoted else ""),
               target=str(bike_id),
               detail={"user_id": user_id, "bike_id": bike_id, "demoted": demoted})
    ctx.conn.commit()
    return {"ok": True, "demoted": demoted}


# ===========================================================================
# PASSWORDS -- your own, and admin's over any account
# ===========================================================================
def _set_password(conn, user_id, password, keep_token=None):
    """Store a new hash and end every other session: a session opened with
    the old password should not outlive it."""
    if len(password) < 8:
        raise HttpError(400, "use at least 8 characters")
    salt = secrets.token_hex(16)
    conn.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
                 (hash_password(password, salt), salt, user_id))
    if keep_token:
        conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?", (user_id, keep_token))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


@route("POST", r"/api/auth/password", role="user")
def change_own_password(ctx):
    """Your own password: the current one to prove it is you, then the new
    one. Other devices are signed out; this one stays."""
    row = one(ctx.conn.execute("SELECT password_hash, password_salt FROM users WHERE id=?",
                               (ctx.user["id"],)))
    current = ctx.body.get("current") or ""
    if not verify_password(current, row["password_hash"], row["password_salt"]):
        raise HttpError(403, "the current password is wrong")
    _set_password(ctx.conn, ctx.user["id"], ctx.body.get("password") or "", keep_token=ctx.user["token"])
    ctx.conn.commit()
    return {"ok": True}


@route("POST", r"/api/admin/users/(\d+)/password", role="admin")
def admin_set_password(ctx):
    """Set anyone's password -- a locked-out member, or the seeded dev
    accounts on a fresh server. They are signed out everywhere."""
    uid = int(ctx.params[0])
    u = one(ctx.conn.execute("SELECT username FROM users WHERE id=?", (uid,)))
    if not u:
        raise HttpError(404, "no such user")
    _set_password(ctx.conn, uid, ctx.body.get("password") or "",
                  keep_token=ctx.user["token"] if uid == ctx.user["id"] else None)
    log_action(ctx, "user.password", f"Set a new password for {u['username']}", target=str(uid))
    ctx.conn.commit()
    return {"ok": True, "username": u["username"]}


@route("POST", r"/api/admin/users/(\d+)/suspend", role="admin")
def admin_suspend(ctx):
    """Stop an account signing in, or let it again. Their votes, flags and
    posts stay; suspending is not deleting."""
    uid = int(ctx.params[0])
    u = one(ctx.conn.execute("SELECT username, role FROM users WHERE id=?", (uid,)))
    if not u:
        raise HttpError(404, "no such user")
    if uid == ctx.user["id"]:
        raise HttpError(400, "not your own account")
    on = bool(ctx.body.get("suspended", True))
    ctx.conn.execute("UPDATE users SET suspended=? WHERE id=?", (1 if on else 0, uid))
    if on:
        ctx.conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    log_action(ctx, "user.suspend" if on else "user.unsuspend",
               f"{'Suspended' if on else 'Reinstated'} {u['username']}", target=str(uid))
    ctx.conn.commit()
    return {"ok": True, "suspended": on}


# ===========================================================================
# BACKUP and RESTORE -- the database and the photos, as files
# ===========================================================================
def _db_snapshot_bytes():
    """A consistent copy of the live database through SQLite's backup API
    (a plain read of the file would miss what is still in the WAL)."""
    import tempfile
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        src = sqlite3.connect(DB_PATH)
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close(); src.close()
        with open(tmp, "rb") as f:
            return f.read()
    finally:
        os.remove(tmp)


@route("GET", r"/api/admin/backup", role="admin")
def admin_backup(ctx):
    """Download the database as of right now."""
    data = _db_snapshot_bytes()
    log_action(ctx, "backup.download", f"Downloaded a database backup ({len(data) // 1024} KB)")
    ctx.conn.commit()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"_file": data, "_filename": f"gearheadspecs-{stamp}.db", "_ctype": "application/x-sqlite3"}


@route("POST", r"/api/admin/restore", role="admin")
def admin_restore(ctx):
    """Replace the live database with an uploaded one -- moving a site to a
    new server, or going back to a backup.

    The file is checked before anything happens: it must be a SQLite
    database that passes integrity_check, carry this app's tables, and hold
    at least one admin account, so a restore can never lock everyone out.
    Then it is copied over the live database through the backup API, in one
    transaction, with readers unaffected. Every session ends with it,
    including this one: the accounts are whatever the restored file says
    they are, so everyone signs in again.
    """
    import tempfile
    raw = ctx.body.get("_raw")
    if not raw:
        raise HttpError(400, "send the database file as the request body (application/octet-stream)")
    if raw[:16] != b"SQLite format 3\x00":
        raise HttpError(400, "that is not a SQLite database file")
    fd, tmp = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
        src = sqlite3.connect(tmp)
        try:
            if src.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise HttpError(400, "the database fails its integrity check")
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            need = {"bikes", "specs", "spec_fields", "users", "sessions"}
            if not need <= tables:
                raise HttpError(400, "the file is a SQLite database but not a GearHeadSpecs one: "
                                     "missing " + ", ".join(sorted(need - tables)))
            admins = src.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND suspended=0").fetchone()[0]
            if not admins:
                raise HttpError(400, "the file has no active admin account -- restoring it would lock everyone out")
            counts = {t: src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("bikes", "specs", "users")}
            # the audit line goes in BEFORE the copy: the live log is about to be replaced
            log_action(ctx, "backup.restore",
                       f"Restored a database file: {counts['bikes']} bikes, {counts['specs']} specs, {counts['users']} users",
                       destructive=True, detail=counts)
            ctx.conn.commit()
            src.backup(ctx.conn)
        finally:
            src.close()
    finally:
        os.remove(tmp)
    ctx.conn.execute("PRAGMA journal_mode=WAL")
    ctx.conn.execute("DELETE FROM sessions")
    ctx.conn.commit()
    return {"ok": True, **counts, "_clear_cookie": True}


@route("POST", r"/api/admin/restore-photos", role="admin")
def admin_restore_photos(ctx):
    """Put a zip of the photos folder back: <bike id>.<jpg|png|webp> files
    only, each checked to really be an image. Anything else in the zip is
    ignored and reported."""
    import io as _io, zipfile
    raw = ctx.body.get("_raw")
    if not raw:
        raise HttpError(400, "send the zip as the request body (application/zip)")
    try:
        z = zipfile.ZipFile(_io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise HttpError(400, "that is not a zip file")
    os.makedirs(PHOTO_DIR, exist_ok=True)
    kept, skipped = [], []
    for info in z.infolist():
        name = os.path.basename(info.filename)
        if info.is_dir() or not re.fullmatch(r"\d+(-\d{4})?\.(jpg|png|webp)", name):
            if not info.is_dir():
                skipped.append(info.filename)
            continue
        data = z.read(info)
        kind = sniff_image(data)
        if not kind or PHOTO_TYPES[kind] != name.rsplit(".", 1)[1]:
            skipped.append(info.filename)
            continue
        with open(os.path.join(PHOTO_DIR, name), "wb") as f:
            f.write(data)
        kept.append(name)
    log_action(ctx, "backup.restore_photos", f"Restored {len(kept)} photo(s)", detail={"skipped": skipped[:50]})
    ctx.conn.commit()
    return {"ok": True, "restored": len(kept), "skipped": skipped[:50]}


@route("GET", r"/api/admin/backup-photos", role="admin")
def admin_backup_photos(ctx):
    """The photos folder as a zip."""
    import io as _io, zipfile
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.isdir(PHOTO_DIR):
            for f in sorted(os.listdir(PHOTO_DIR)):
                if re.fullmatch(r"\d+(-\d{4})?\.(jpg|png|webp)", f):
                    z.write(os.path.join(PHOTO_DIR, f), f)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {"_file": buf.getvalue(), "_filename": f"gearheadspecs-photos-{stamp}.zip", "_ctype": "application/zip"}


# ===========================================================================
# SIGN IN AS -- admin testing the site as a member
# ===========================================================================
@route("POST", r"/api/admin/users/(\d+)/impersonate", role="admin")
def impersonate(ctx):
    """Become another member for a while, to see what they see.

    A short session is opened for them and handed to the browser; admin's
    own session token waits in a second cookie so "back to admin" is one
    click and needs no password. Logged, with who and for how long: acting
    as somebody else is exactly the kind of thing the audit log is for.
    Never another admin, never a suspended account.
    """
    uid = int(ctx.params[0])
    u = one(ctx.conn.execute("SELECT id, username, role, suspended FROM users WHERE id=?", (uid,)))
    if not u:
        raise HttpError(404, "no such user")
    if u["id"] == ctx.user["id"]:
        raise HttpError(400, "that is you")
    if u["role"] == "admin":
        raise HttpError(403, "not another admin account")
    if u["suspended"]:
        raise HttpError(409, "that account is suspended")
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(hours=IMPERSONATE_HOURS)
    ctx.conn.execute("INSERT INTO sessions (token, user_id, expires_at) VALUES (?,?,?)",
                     (token, u["id"], expires.strftime("%Y-%m-%d %H:%M:%S")))
    log_action(ctx, "user.impersonate", f"Signed in as {u['username']} ({u['role']}) for testing",
               target=str(uid), detail={"hours": IMPERSONATE_HOURS})
    ctx.conn.commit()
    return {"ok": True, "username": u["username"], "role": u["role"],
            "_set_cookie": token, "_set_admin_cookie": ctx.user["token"]}


@route("POST", r"/api/auth/return", role="user")
def return_to_admin(ctx):
    """Back from a test sign-in: the test session ends, admin's own comes
    back. Refused unless the waiting token is a live admin session."""
    back = user_for_token(ctx.conn, getattr(ctx, "admin_token", None))
    if not back or back["role"] != "admin":
        raise HttpError(403, "no admin session to return to")
    ctx.conn.execute("DELETE FROM sessions WHERE token=?", (ctx.user["token"],))
    ctx.conn.commit()
    return {"ok": True, "username": back["username"],
            "_set_cookie": back["token"], "_clear_admin_cookie": True}


@route("GET", r"/api/users", role="admin")
def list_users(ctx):
    """Every account, with the email. Admin only -- this is the one place the
    address is returned; user_profile_stats, which the public profile reads,
    deliberately does not carry it."""
    users = rows(ctx.conn.execute(
        "SELECT p.*, u.email, u.suspended, u.created_at,"
        " (SELECT COUNT(*) FROM bike_managers m WHERE m.user_id = u.id) AS bikes_managed,"
        " (SELECT GROUP_CONCAT(d.display_name || COALESCE(' (' || d.year_range || ')', ''), '|')"
        "    FROM bike_managers m JOIN bike_display d ON d.bike_id = m.bike_id"
        "   WHERE m.user_id = u.id) AS bikes"
        " FROM user_profile_stats p JOIN users u ON u.id = p.user_id"
        " ORDER BY u.created_at DESC, u.username"))
    for u in users:
        u["bikes"] = [b for b in (u.pop("bikes") or "").split("|") if b]
        if u["bikes"] or u["role"] == "admin":
            st = manager_standing(ctx.conn, u["user_id"])
            u["tier"], u["gold_eligible"], u["gold_confirmed"] = st["tier"], st["gold_eligible"], st["gold_confirmed"]
        row = ctx.conn.execute("SELECT founder, retired_tier FROM users WHERE id=?", (u["user_id"],)).fetchone()
        u["founder"], u["retired_tier"] = bool(row[0]), row[1]
    return {"users": users}


# ===========================================================================
# HTTP plumbing
# ===========================================================================
class Handler(BaseHTTPRequestHandler):
    server_version = "GearHeadSpecs"

    def log_message(self, fmt, *args):
        pass

    # -- helpers ----------------------------------------------------------
    def _send_file(self, data, filename, ctype=None):
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, status, payload, cookie=None, clear_cookie=False,
                   admin_cookie=None, clear_admin=False):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if admin_cookie:
            self.send_header("Set-Cookie",
                             f"{ADMIN_COOKIE}={admin_cookie}; HttpOnly; SameSite=Lax;"
                             f" Path=/; Max-Age={IMPERSONATE_HOURS * 3600}")
        if clear_admin:
            self.send_header("Set-Cookie",
                             f"{ADMIN_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
        # No CORS header: the pages are served from this same origin, and a
        # permissive one alongside cookie auth is a liability, not a feature.
        if cookie:
            self.send_header("Set-Cookie",
                             f"{COOKIE_NAME}={cookie}; HttpOnly; SameSite=Lax;"
                             f" Path=/; Max-Age={SESSION_DAYS * 86400}")
        if clear_cookie:
            self.send_header("Set-Cookie",
                             f"{COOKIE_NAME}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0")
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self, name=COOKIE_NAME):
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        try:
            return SimpleCookie(raw).get(name).value
        except Exception:
            return None

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        # A database file or a zip of photos, for admin's restore: raw, big.
        if ctype in ("application/octet-stream", "application/zip", "application/x-zip-compressed"):
            if length > RESTORE_MAX_BYTES:
                raise HttpError(413, f"the file is over {RESTORE_MAX_BYTES // (1024 * 1024)} MB")
            return {"_raw": self.rfile.read(length), "_ctype": ctype}
        # An image comes as itself. It is handed to the route as bytes under
        # "_raw"; the route decides what it really is from the bytes.
        if ctype.startswith("image/"):
            if length > PHOTO_MAX_BYTES:
                # Drain what the client is still sending, within reason, so the
                # 413 reaches it instead of a reset connection. Past that it
                # is not a photo, and the connection can drop.
                left = min(length, PHOTO_MAX_BYTES * 3)
                while left > 0:
                    left -= len(self.rfile.read(min(left, 65536)) or b"x")
                raise HttpError(413, f"the photo is over {PHOTO_MAX_BYTES // (1024 * 1024)} MB")
            return {"_raw": self.rfile.read(length), "_ctype": ctype}
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HttpError(400, "body must be valid JSON")

    # -- dispatch ---------------------------------------------------------
    def _handle(self, method):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if not path.startswith("/api/"):
            return self._serve_static(path)

        conn = db()
        try:
            # DELETE included: the destructive removals carry their confirmation
            # in the body ({"force": true}). Without reading it here the flag was
            # dropped silently and every confirmed delete came back refused.
            body = (self._read_body()
                    if method in ("POST", "PATCH", "PUT", "DELETE") else {})
            user = user_for_token(conn, self._cookie_token())

            matched_path = False
            for m, pattern, fn, role in ROUTES:
                hit = pattern.match(path)
                if not hit:
                    continue
                matched_path = True
                if m != method:
                    continue

                if role != "anon":
                    if not user:
                        raise HttpError(401, "sign in to do that")
                    if user["suspended"]:
                        raise HttpError(403, "this account is suspended")
                    if ROLE_RANK[user["role"]] < ROLE_RANK.get(role, 0):
                        raise HttpError(403, f"requires {role} access")

                ctx = Ctx(conn, user, hit.groups(), parse_qs(parsed.query), body)
                ctx.admin_token = self._cookie_token(ADMIN_COOKIE)
                result = fn(ctx)
                if isinstance(result, dict) and "_file" in result:
                    return self._send_file(result["_file"], result["_filename"], result.get("_ctype"))
                cookie = result.pop("_set_cookie", None) if isinstance(result, dict) else None
                clear = result.pop("_clear_cookie", False) if isinstance(result, dict) else False
                admin_cookie = result.pop("_set_admin_cookie", None) if isinstance(result, dict) else None
                clear_admin = result.pop("_clear_admin_cookie", False) if isinstance(result, dict) else False
                return self._send_json(200, result, cookie=cookie, clear_cookie=clear,
                                       admin_cookie=admin_cookie, clear_admin=clear_admin)

            if matched_path:
                raise HttpError(405, f"{method} not allowed on this path")
            raise HttpError(404, "no such endpoint")

        except HttpError as e:
            self._send_json(e.status, {"error": e.message})
        except sqlite3.IntegrityError as e:
            self._send_json(409, {"error": f"database constraint: {e}"})
        except Exception as e:  # noqa: BLE001 - last resort, keeps the server up
            import traceback
            traceback.print_exc()
            self._send_json(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            conn.close()

    def _serve_static(self, path):
        rel = path.lstrip("/") or "index.html"
        # Bike photos live with the data, not the code, and are served from
        # there under the same traversal guard.
        base = STATIC_DIR
        if rel.startswith("photos/"):
            base, rel = PHOTO_DIR, rel[len("photos/"):]
        full = os.path.normpath(os.path.join(base, rel))
        # normpath first, then confirm we are still inside the served directory:
        # without this a path of ../../ walks straight out of it.
        if not full.startswith(base + os.sep) and full != base:
            self._send_json(403, {"error": "forbidden"})
            return
        if os.path.isdir(full):
            full = os.path.join(full, "index.html")
        if not os.path.isfile(full):
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"404 - not found")
            return
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):    self._handle("GET")
    def do_POST(self):   self._handle("POST")
    def do_PATCH(self):  self._handle("PATCH")
    def do_DELETE(self): self._handle("DELETE")


def main():
    if not os.path.exists(DB_PATH):
        raise SystemExit("data.db not found — run:  py seed.py")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"GearHeadSpecs running on http://{HOST}:{PORT}/")
    print("Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
