"""
End-to-end API tests. Spins up the real server against a throwaway copy of
data.db, so nothing here can damage the working database.

Run:  py test_api.py
"""
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from http.cookiejar import CookieJar

import app
import questionnaire
import wire_colors
import seed

ROOT = os.path.dirname(os.path.abspath(__file__))


def build_test_db(path):
    """Fresh seed into a temp file rather than copying data.db, so the tests
    describe the seeder's output and not whatever state a manual poke left."""
    real_db = seed.DB_PATH
    seed.DB_PATH = path
    try:
        seed.main()
    finally:
        seed.DB_PATH = real_db


class Client:
    """One browser. Its own cookie jar, so sessions don't leak between roles."""

    def __init__(self, base):
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with self.opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw or b"{}")
            except json.JSONDecodeError:
                return e.code, {"raw": raw.decode(errors="replace")}

    def get(self, p):            return self.call("GET", p)
    def post(self, p, b=None):   return self.call("POST", p, b or {})
    def patch(self, p, b=None):  return self.call("PATCH", p, b or {})
    def delete(self, p, b=None): return self.call("DELETE", p, b)

    def login(self, username, password="gearhead"):
        return self.post("/api/auth/login",
                         {"username": username, "password": password})


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="ghs_test_")
        cls.db = os.path.join(cls.tmp, "test.db")
        build_test_db(cls.db)
        app.DB_PATH = cls.db
        app.PHOTO_DIR = os.path.join(cls.tmp, "photos")

        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

        con = sqlite3.connect(cls.db)
        cls.cb919 = con.execute(
            "SELECT id FROM bikes WHERE model_code='CB900F2 919'").fetchone()[0]
        con.close()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def anon(self):    return Client(self.base)
    def as_(self, u):
        c = Client(self.base)
        status, _ = c.login(u)
        self.assertEqual(status, 200, f"login failed for {u}")
        return c

    # -- auth -------------------------------------------------------------
    def test_01_login_rejects_bad_password(self):
        s, b = self.anon().login("m.alvarez", "wrong")
        self.assertEqual(s, 401)
        self.assertNotIn("m.alvarez", json.dumps(b))

    def test_02_login_unknown_user_same_error(self):
        s1, b1 = self.anon().login("nobody-here", "x")
        s2, b2 = self.anon().login("m.alvarez", "wrong")
        self.assertEqual((s1, b1), (s2, b2),
                         "unknown user and wrong password must be indistinguishable")

    def test_03_me_anonymous(self):
        s, b = self.anon().get("/api/auth/me")
        self.assertEqual(s, 200)
        self.assertIsNone(b["user"])

    def test_04_session_survives_and_logout_clears(self):
        c = self.as_("m.alvarez")
        s, b = c.get("/api/auth/me")
        self.assertEqual(b["user"]["username"], "m.alvarez")
        self.assertIn(self.cb919, b["manages"])
        c.post("/api/auth/logout")
        s, b = c.get("/api/auth/me")
        self.assertIsNone(b["user"])

    # -- role gates -------------------------------------------------------
    def test_10_anon_blocked_from_manager(self):
        self.assertEqual(self.anon().get("/api/manager/flags")[0], 401)

    def test_11_user_blocked_from_manager(self):
        self.assertEqual(self.as_("t.moreno").get("/api/manager/flags")[0], 403)

    def test_12_manager_blocked_from_admin(self):
        self.assertEqual(self.as_("m.alvarez").get("/api/admin/summary")[0], 403)

    def test_13_admin_reaches_admin(self):
        self.assertEqual(self.as_("admin").get("/api/admin/summary")[0], 200)

    def test_14_manager_cannot_fix_a_bike_they_do_not_manage(self):
        """The core rule: manager rank alone is not enough, it must be
        this manager's bike."""
        con = sqlite3.connect(self.db)
        con.execute("INSERT INTO users (username, role, password_hash, password_salt)"
                    " VALUES ('other_mgr','manager','x','y')")
        con.commit()
        uid = con.execute("SELECT id FROM users WHERE username='other_mgr'").fetchone()[0]
        # Give them a real password through the same path the seeder uses.
        h, salt = seed.hash_password("gearhead")
        con.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?",
                    (h, salt, uid))
        con.commit()
        flag_id = con.execute("SELECT id FROM value_flags WHERE status='open'").fetchone()[0]
        con.close()

        s, b = self.as_("other_mgr").post(f"/api/flags/{flag_id}/fix",
                                          {"new_value": "hijacked"})
        self.assertEqual(s, 403)
        self.assertIn("do not manage", b["error"])

    # -- anonymous visitors ------------------------------------------------
    #
    # "Public" means not signed in. Such a visitor reads the whole database and
    # writes none of it. These tests are the server-side half of that: the
    # pages hide nothing and let every control be clicked, so the only thing
    # actually stopping an anonymous write is the route's role gate.
    def test_05_anonymous_can_read_everything_public(self):
        a = self.anon()
        for path in (f"/api/bikes?limit=3",
                     f"/api/bikes/{self.cb919}",
                     f"/api/bikes/{self.cb919}/specs",
                     f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment",
                     "/api/catalog/filters"):
            self.assertEqual(a.get(path)[0], 200, f"anon should read {path}")

    def test_06_anonymous_sees_values_not_just_names(self):
        """A first-time visitor arriving from a search engine has to get the
        actual answer, or the site is useless to them."""
        _, b = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in b["categories"] for sp in c["specs"]]
        self.assertTrue([sp for sp in flat if sp["value"]], "no values visible")
        alts = [a for sp in flat for a in sp["alternates"]]
        self.assertTrue(alts, "alternates hidden from anonymous")
        self.assertTrue(any(a["votes"] for a in alts), "vote counts hidden")

    def test_07_anonymous_cannot_write_anything(self):
        a = self.anon()
        _, specs = a.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        spec = flat[0]
        gap = next((sp for sp in flat if not sp["value"]), spec)
        alt = next((x for sp in flat for x in sp["alternates"]), None)
        _, enr = a.get(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")

        attempts = [
            ("POST", f"/api/specs/{gap['id']}/value", {"value": "drive-by"}),
            ("POST", f"/api/specs/{spec['id']}/flags", {"reason": "incorrect"}),
            ("POST", f"/api/specs/{spec['id']}/alternates", {"text": "drive-by"}),
            ("POST", f"/api/alternates/{alt['id']}/vote", {}),
            ("POST", f"/api/links/{enr['links'][0]['id']}/vote", {}),
            ("POST", f"/api/links/{enr['links'][0]['id']}/flag", {"reason": "spam"}),
            ("POST", f"/api/bikes/{self.cb919}/fields/engine_oil_volume/tools",
             {"text": "drive-by"}),
            ("POST", f"/api/bikes/{self.cb919}/fields/engine_oil_volume/links",
             {"link_type": "yt", "title": "x", "url": "https://example.com/x"}),
            ("GET", "/api/garage", None),
            ("POST", "/api/garage", {"bike_year_id": 1}),
        ]
        for method, path, body in attempts:
            status, _ = a.call(method, path, body)
            self.assertEqual(status, 401, f"anon reached {method} {path}")

    def test_08_anonymous_write_leaves_no_trace(self):
        """Belt and braces: the refusals above must not have half-applied."""
        a = self.anon()
        _, specs = a.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        gap = next(sp for sp in flat if not sp["value"])
        a.post(f"/api/specs/{gap['id']}/value", {"value": "drive-by"})
        _, again = a.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for c in again["categories"] for sp in c["specs"]
                     if sp["id"] == gap["id"])
        self.assertIn(after["value"], (None, ""))

    def test_09_no_stored_role_is_called_public(self):
        """'public' now means 'not signed in'. A stored role by that name would
        make every permission check ambiguous to read."""
        con = sqlite3.connect(self.db)
        roles = {r[0] for r in con.execute("SELECT DISTINCT role FROM users")}
        ddl = con.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        con.close()
        self.assertNotIn("public", roles)
        self.assertEqual(roles, {"user", "manager", "admin"} & roles)
        self.assertNotIn("'public'", ddl, "CHECK constraint still allows 'public'")

    # -- browse -----------------------------------------------------------
    def test_19_models_are_ordered_by_engine_size(self):
        """The mockup labels this dropdown "Model (ordered by CC)". An
        alphabetical list interleaves a 50cc scooter with a litre bike, which is
        useless to someone scanning for a size."""
        _, f = self.anon().get("/api/catalog/filters")
        self.assertTrue(f["models"], "no models returned")
        self.assertIn("model_code", f["models"][0])

        ccs = [m["cc"] for m in f["models"] if m["cc"] is not None]
        self.assertGreater(len(ccs), 50, "hardly any displacements parsed")
        self.assertEqual(ccs, sorted(ccs), "models are not in ascending cc order")

        # Models with no displacement recorded sort last rather than as 0cc.
        known = [i for i, m in enumerate(f["models"]) if m["cc"] is not None]
        unknown = [i for i, m in enumerate(f["models"]) if m["cc"] is None]
        if unknown:
            self.assertGreater(min(unknown), max(known),
                               "unknown-cc models are not last")

    def test_20_bikes_list(self):
        s, b = self.anon().get("/api/bikes?limit=5")
        self.assertEqual(s, 200)
        self.assertEqual(len(b["bikes"]), 5)
        self.assertGreater(b["total"], 200)

    def test_21_search_finds_bike_by_alternate_name(self):
        """'Hornet 900' is an EU name for the CB919. Names are data, so
        searching either one must land on the same bike id."""
        c = self.anon()
        _, hornet = c.get("/api/bikes?q=Hornet")
        _, cb = c.get("/api/bikes?q=CB919")
        self.assertTrue(hornet["bikes"], "no result for Hornet 900")
        self.assertTrue(cb["bikes"], "no result for CB919")
        self.assertEqual(hornet["bikes"][0]["bike_id"], cb["bikes"][0]["bike_id"])

    def test_22_bike_detail(self):
        s, b = self.anon().get(f"/api/bikes/{self.cb919}")
        self.assertEqual(s, 200)
        self.assertEqual(b["display_name"], "Honda CB919")
        self.assertEqual(b["year_range"], "2002-2007")
        self.assertEqual(len(b["years"]), 6)
        self.assertEqual(len(b["names"]), 4)

    def test_23_bike_404(self):
        self.assertEqual(self.anon().get("/api/bikes/999999")[0], 404)

    def test_24_specs_grouped_with_alternates(self):
        s, b = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        self.assertEqual(s, 200)
        cats = {c["name"] for c in b["categories"]}
        self.assertIn("Engine", cats)
        flat = [sp for c in b["categories"] for sp in c["specs"]]
        lube = next(sp for sp in flat if sp["label"] == "Chain Lube (type/brand)")
        self.assertEqual(len(lube["alternates"]), 3)
        self.assertGreater(lube["alternates"][0]["votes"], 0)

    def test_25_null_spec_is_a_gap_not_a_guess(self):
        _, b = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in b["categories"] for sp in c["specs"]]
        battery = next(sp for sp in flat if sp["label"] == "Battery")
        self.assertIsNone(battery["value"])
        self.assertEqual(battery["confidence"], "pending")

    # -- community --------------------------------------------------------
    def test_30_vote_toggles_and_does_not_double_count(self):
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for cat in specs["categories"] for sp in cat["specs"]]
        alt = next(sp for sp in flat if sp["alternates"])["alternates"][0]
        start = alt["votes"]

        s, b1 = c.post(f"/api/alternates/{alt['id']}/vote")
        self.assertEqual(s, 200)
        s, b2 = c.post(f"/api/alternates/{alt['id']}/vote")   # same user again
        s, b3 = c.post(f"/api/alternates/{alt['id']}/vote")   # and back on

        self.assertTrue(b1["voted"]);  self.assertEqual(b1["votes"], start + 1)
        self.assertFalse(b2["voted"]); self.assertEqual(b2["votes"], start)
        self.assertTrue(b3["voted"]);  self.assertEqual(b3["votes"], start + 1)

    def _empty_spec(self, client, bike_id=None):
        """A spec with no value.

        Several tests fill gaps, so the supply runs out as the suite proceeds
        and which test hits empty depends on execution order — a flaky
        dependency on data rather than on behaviour. If none are left, clear
        one through the manager API (a manager removing a wrong value is a real
        operation) so the caller always gets a genuine gap.
        """
        bike_id = bike_id or self.cb919
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        # Callers type free text into the gap. A typed field (wire colour,
        # octane grade, ethanol) refuses anything off its list, so only a
        # plain-text field is a gap for this purpose.
        flat = [sp for c in specs["categories"] for sp in c["specs"]
                if sp.get("value_type", "text") == "text"]
        gap = next((sp for sp in flat if sp["value"] in (None, "")), None)
        if gap:
            return gap

        mgr = self.as_("m.alvarez")
        victim = next(sp for sp in flat if sp["value"])
        s, b = mgr.patch(f"/api/specs/{victim['id']}",
                         {"value": "", "confidence": "pending"})
        self.assertEqual(s, 200, f"could not clear a spec to make a gap: {b}")
        _, again = client.get(f"/api/bikes/{bike_id}/specs")
        return next(sp for c in again["categories"] for sp in c["specs"]
                    if sp["id"] == victim["id"])

    def _filled_spec(self, client):
        _, specs = client.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        return next(sp for sp in flat if sp["value"])

    # -- requesting a gap -------------------------------------------------
    def test_26_request_a_gap_and_withdraw(self):
        """The demand signal: which of a manager's empty fields anyone wants."""
        c = self.as_("t.moreno")
        spec = self._empty_spec(c)
        self.assertEqual(spec["requests"], 0)
        self.assertFalse(spec["my_request"])

        s, b = c.post(f"/api/specs/{spec['id']}/request")
        self.assertEqual(s, 200)
        self.assertTrue(b["requested"]);  self.assertEqual(b["requests"], 1)

        _, again = c.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for cat in again["categories"] for sp in cat["specs"]
                     if sp["id"] == spec["id"])
        self.assertEqual(after["requests"], 1)
        self.assertTrue(after["my_request"])

        _, off = c.post(f"/api/specs/{spec['id']}/request")
        self.assertFalse(off["requested"]); self.assertEqual(off["requests"], 0)

    def test_27_requests_do_not_double_count(self):
        c1, c2 = self.as_("t.moreno"), self.as_("rider_kestrel99")
        spec = self._empty_spec(c1)
        c1.post(f"/api/specs/{spec['id']}/request")
        c1.post(f"/api/specs/{spec['id']}/request")   # toggles off
        c1.post(f"/api/specs/{spec['id']}/request")   # back on
        _, b = c2.post(f"/api/specs/{spec['id']}/request")
        self.assertEqual(b["requests"], 2, "one request per person")

    def test_28_cannot_request_a_spec_that_has_a_value(self):
        c = self.as_("t.moreno")
        spec = self._filled_spec(c)
        s, b = c.post(f"/api/specs/{spec['id']}/request")
        self.assertEqual(s, 400)
        self.assertIn("already has a value", b["error"])

    def test_29_requests_reach_the_manager_ranked(self):
        c1, c2 = self.as_("t.moreno"), self.as_("rider_kestrel99")
        _, specs = c1.get(f"/api/bikes/{self.cb919}/specs")
        # Gaps nobody has requested yet. requests == 0 guarantees neither of
        # these two users has one outstanding, so the posts below add rather
        # than toggling off something an earlier test left behind.
        gaps = [sp for cat in specs["categories"] for sp in cat["specs"]
                if not sp["value"] and sp["requests"] == 0][:2]
        self.assertEqual(len(gaps), 2, "need two untouched gaps")
        # two people want the first, one wants the second
        c1.post(f"/api/specs/{gaps[0]['id']}/request")
        c2.post(f"/api/specs/{gaps[0]['id']}/request")
        c1.post(f"/api/specs/{gaps[1]['id']}/request")

        _, q = self.as_("m.alvarez").get("/api/manager/requests")
        ids = [r["spec_id"] for r in q["requests"]]
        self.assertIn(gaps[0]["id"], ids)
        counts = [r["requests"] for r in q["requests"]]
        self.assertEqual(counts, sorted(counts, reverse=True),
                         "most-wanted must come first")
        by_id = {r["spec_id"]: r["requests"] for r in q["requests"]}
        self.assertEqual(by_id[gaps[0]["id"]], 2)
        self.assertEqual(by_id[gaps[1]["id"]], 1)

    # -- voting the stock value -------------------------------------------
    def test_2a_stock_value_is_votable(self):
        """Agreement with the manual had nowhere to go before this."""
        c = self.as_("t.moreno")
        spec = self._filled_spec(c)
        s, b = c.post(f"/api/specs/{spec['id']}/vote")
        self.assertEqual(s, 200)
        self.assertTrue(b["voted"]); self.assertEqual(b["votes"], 1)
        _, off = c.post(f"/api/specs/{spec['id']}/vote")
        self.assertFalse(off["voted"]); self.assertEqual(off["votes"], 0)

    def test_2b_cannot_vote_on_an_empty_value(self):
        c = self.as_("t.moreno")
        s, b = c.post(f"/api/specs/{self._empty_spec(c)['id']}/vote")
        self.assertEqual(s, 400)
        self.assertIn("no value here", b["error"])

    # -- flagging one alternate -------------------------------------------
    def test_2c_alternate_carries_its_own_flags(self):
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        spec = next(sp for cat in specs["categories"] for sp in cat["specs"]
                    if sp["alternates"])
        alt = spec["alternates"][0]
        self.assertEqual(alt["flags"], 0)

        s, _ = c.post(f"/api/alternates/{alt['id']}/flag", {"reason": "incorrect"})
        self.assertEqual(s, 200)
        self.assertEqual(c.post(f"/api/alternates/{alt['id']}/flag",
                                {"reason": "incorrect"})[0], 409)

        _, again = c.get(f"/api/bikes/{self.cb919}/specs")
        sp2 = next(x for cat in again["categories"] for x in cat["specs"]
                   if x["id"] == spec["id"])
        a2 = next(a for a in sp2["alternates"] if a["id"] == alt["id"])
        self.assertEqual(a2["flags"], 1)
        self.assertTrue(a2["my_flag"])
        # The flag belongs to the alternate, not to the spec's stock value.
        self.assertEqual(sp2["open_flags"], spec["open_flags"])

    def test_2c2_flagging_the_stock_value_marks_it_as_yours(self):
        """After you flag a value the icon has to show as flagged, which needs
        my_flag on the spec — the alternates carried it, the stock value did
        not, so a raised flag looked unraised on reload."""
        c = self.as_("t.moreno")
        spec = self._filled_spec(c)
        self.assertIn("my_flag", spec, "spec payload is missing my_flag")
        self.assertFalse(spec["my_flag"])

        s, _ = c.post(f"/api/specs/{spec['id']}/flags",
                      {"reason": "incorrect", "detail": "mine"})
        self.assertEqual(s, 200)

        _, again = c.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for cat in again["categories"] for sp in cat["specs"]
                     if sp["id"] == spec["id"])
        self.assertTrue(after["my_flag"], "own flag not reported back")
        self.assertEqual(after["open_flags"], spec["open_flags"] + 1)

        # Somebody else's flag must not read as yours.
        _, other = self.as_("rider_kestrel99").get(f"/api/bikes/{self.cb919}/specs")
        theirs = next(sp for cat in other["categories"] for sp in cat["specs"]
                      if sp["id"] == spec["id"])
        self.assertFalse(theirs["my_flag"])
        self.assertEqual(theirs["open_flags"], after["open_flags"])

    def test_2c3_one_open_flag_per_person_per_value(self):
        c = self.as_("rider_kestrel99")
        spec = self._filled_spec(c)
        first = c.post(f"/api/specs/{spec['id']}/flags", {"reason": "other"})[0]
        s, b = c.post(f"/api/specs/{spec['id']}/flags", {"reason": "other"})
        self.assertIn(first, (200, 409))
        self.assertEqual(s, 409)
        self.assertIn("already flagged", b["error"])

    def test_2d_fixing_an_alternate_flag_is_refused(self):
        """Fix Value writes the stock value. Doing that for a flag aimed at
        somebody's alternate would silently overwrite the manual."""
        c = self.as_("rider_kestrel99")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        spec = next(sp for cat in specs["categories"] for sp in cat["specs"]
                    if sp["alternates"])
        alt = spec["alternates"][0]
        _, made = c.post(f"/api/alternates/{alt['id']}/flag", {"reason": "other"})

        mgr = self.as_("m.alvarez")
        s, b = mgr.post(f"/api/flags/{made['id']}/fix", {"new_value": "clobbered"})
        self.assertEqual(s, 400)
        self.assertIn("against an alternate", b["error"])

        _, after = mgr.get(f"/api/bikes/{self.cb919}/specs")
        sp2 = next(x for cat in after["categories"] for x in cat["specs"]
                   if x["id"] == spec["id"])
        self.assertEqual(sp2["value"], spec["value"], "stock value was changed")
        # Dismissing it is the right move, and still works.
        self.assertEqual(mgr.post(f"/api/flags/{made['id']}/dismiss")[0], 200)

    def test_2e_anonymous_cannot_request_or_vote_stock(self):
        a = self.anon()
        _, specs = a.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        gap = next(sp for sp in flat if not sp["value"])
        filled = next(sp for sp in flat if sp["value"])
        self.assertEqual(a.post(f"/api/specs/{gap['id']}/request")[0], 401)
        self.assertEqual(a.post(f"/api/specs/{filled['id']}/vote")[0], 401)

    def test_36_user_can_fill_an_empty_spec(self):
        """The point of a community database: a rider who knows the value can
        put it in without waiting for the assigned manager."""
        c = self.as_("t.moreno")
        spec = self._empty_spec(c)
        s, b = c.post(f"/api/specs/{spec['id']}/value", {"value": "Yuasa YTX12-BS"})
        self.assertEqual(s, 200, b)

        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for cat in specs["categories"] for sp in cat["specs"]
                     if sp["id"] == spec["id"])
        self.assertEqual(after["value"], "Yuasa YTX12-BS")
        # Sourced by a person, not confirmed by the expert. Shown as Unsourced.
        self.assertEqual(after["confidence"], "pending")
        self.assertEqual(after["entered_by_username"], "t.moreno")

    def test_37_user_cannot_overwrite_an_existing_value(self):
        """Filling a blank is additive. Overwriting is destructive, and stays a
        flag so the existing value survives until a human decides."""
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        filled = next(sp for cat in specs["categories"] for sp in cat["specs"]
                      if sp["value"])
        before = filled["value"]
        s, b = c.post(f"/api/specs/{filled['id']}/value", {"value": "vandalised"})
        self.assertEqual(s, 409)
        self.assertIn("already has a value", b["error"])

        _, again = c.get(f"/api/bikes/{self.cb919}/specs")
        still = next(sp for cat in again["categories"] for sp in cat["specs"]
                     if sp["id"] == filled["id"])
        self.assertEqual(still["value"], before)

    def test_38_second_filler_does_not_clobber_the_first(self):
        c1, c2 = self.as_("t.moreno"), self.as_("rider_kestrel99")
        spec = self._empty_spec(c1)
        self.assertEqual(
            c1.post(f"/api/specs/{spec['id']}/value", {"value": "first in"})[0], 200)
        s, b = c2.post(f"/api/specs/{spec['id']}/value", {"value": "second in"})
        self.assertEqual(s, 409)

        _, specs = c1.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for cat in specs["categories"] for sp in cat["specs"]
                     if sp["id"] == spec["id"])
        self.assertEqual(after["value"], "first in")

    def test_39_submission_reaches_the_manager_and_confirming_clears_it(self):
        pub = self.as_("t.moreno")
        spec = self._empty_spec(pub)
        pub.post(f"/api/specs/{spec['id']}/value", {"value": "community answer"})

        mgr = self.as_("m.alvarez")
        _, q = mgr.get("/api/manager/submissions")
        row = next(x for x in q["submissions"] if x["spec_id"] == spec["id"])
        self.assertEqual(row["entered_by"], "t.moreno")
        self.assertEqual(row["value"], "community answer")

        # Confirming is raising the confidence, which is also what dequeues it.
        self.assertEqual(mgr.patch(f"/api/specs/{spec['id']}", {"confidence": "mfr"})[0], 200)
        _, after = mgr.get("/api/manager/submissions")
        self.assertNotIn(spec["id"], [x["spec_id"] for x in after["submissions"]])

    def test_39a_confirming_keeps_the_contributor_credited(self):
        """Confirming says 'I checked this', not 'I wrote this'. Reassigning
        entered_by would strip the rider's credit and move the contribution
        onto the manager's profile stats."""
        pub = self.as_("t.moreno")
        spec = self._empty_spec(pub)
        status, body = pub.post(f"/api/specs/{spec['id']}/value",
                                {"value": "rider's answer"})
        self.assertEqual(status, 200, f"the fill itself failed: {body}")
        _, me = pub.get("/api/auth/me")
        before = self.anon().get(f"/api/users/{me['user']['id']}/profile")[1]["specs_entered"]

        mgr = self.as_("m.alvarez")
        mgr.patch(f"/api/specs/{spec['id']}", {"confidence": "confirmed"})

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for c in specs["categories"] for sp in c["specs"]
                     if sp["id"] == spec["id"])
        self.assertEqual(after["entered_by_username"], "t.moreno")
        self.assertEqual(after["confidence"], "confirmed")
        self.assertEqual(
            self.anon().get(f"/api/users/{me['user']['id']}/profile")[1]["specs_entered"],
            before, "confirming must not move the credit")

    def test_39a2_correcting_the_value_does_move_authorship(self):
        pub = self.as_("t.moreno")
        spec = self._empty_spec(pub)
        pub.post(f"/api/specs/{spec['id']}/value", {"value": "wrong guess"})

        mgr = self.as_("m.alvarez")
        s, b = mgr.patch(f"/api/specs/{spec['id']}",
                         {"value": "the right answer", "confidence": "confirmed"})
        self.assertTrue(b["value_changed"])
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for c in specs["categories"] for sp in c["specs"]
                     if sp["id"] == spec["id"])
        self.assertEqual(after["entered_by_username"], "m.alvarez")

    def test_39b_managers_own_entries_are_not_submissions(self):
        """The queue is for values from other people. A manager's own pending
        entry is not something they need to review."""
        mgr = self.as_("m.alvarez")
        spec = self._empty_spec(mgr)
        mgr.post(f"/api/questionnaire/{self.cb919}/answer", {
            "field_key": spec["field_key"], "value": "mine", "confidence": "pending"})
        _, q = mgr.get("/api/manager/submissions")
        self.assertNotIn(spec["id"], [x["spec_id"] for x in q["submissions"]])

    def test_39c_user_cannot_edit_a_spec_directly(self):
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        spec = specs["categories"][0]["specs"][0]
        self.assertEqual(c.patch(f"/api/specs/{spec['id']}", {"value": "nope"})[0], 403)
        self.assertEqual(self.anon().post(f"/api/specs/{spec['id']}/value",
                                          {"value": "nope"})[0], 401)

    # -- the manager's Fixed-spec tag -------------------------------------
    def _open_spec(self, client):
        """A spec that is not Fixed, so contributions are allowed.

        Also skips ones this user has already flagged. Since 'pref' became the
        default nearly every field is open, so this returns the FIRST spec
        rather than one of the handful that used to be non-fixed — and that
        one may already carry this user's flag from an earlier test.
        """
        _, specs = client.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        return next(sp for sp in flat
                    if sp["spec_type"] != "fixed" and not sp["my_flag"])

    def test_2f_manager_can_add_and_remove_the_fixed_tag(self):
        mgr = self.as_("m.alvarez")
        spec = self._open_spec(mgr)
        self.assertFalse(spec["type_overridden"])

        s, b = mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": "fixed"})
        self.assertEqual(s, 200, b)
        self.assertEqual(b["spec_type"], "fixed")
        self.assertTrue(b["overridden"])

        _, after = mgr.get(f"/api/bikes/{self.cb919}/specs")
        got = next(sp for c in after["categories"] for sp in c["specs"]
                   if sp["id"] == spec["id"])
        self.assertEqual(got["spec_type"], "fixed")
        self.assertTrue(got["type_overridden"])

        # null clears the override and the field falls back to the tree default
        s, b = mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": None})
        self.assertEqual(s, 200)
        self.assertFalse(b["overridden"])
        self.assertEqual(b["spec_type"], spec["field_spec_type"])

    def test_2g_fixed_closes_the_spec_to_everyone_else(self):
        """"If a spec has that, no one can add anything." Alternates and
        community values both stop; flagging stays open."""
        mgr = self.as_("m.alvarez")
        pub = self.as_("t.moreno")
        spec = self._open_spec(pub)

        # open: a user can propose an alternate
        self.assertEqual(
            pub.post(f"/api/specs/{spec['id']}/alternates", {"text": "before"})[0], 200)

        mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": "fixed"})

        s, b = pub.post(f"/api/specs/{spec['id']}/alternates", {"text": "after"})
        self.assertEqual(s, 400)
        self.assertIn("single correct value", b["error"])

        # flagging is still allowed - disagreeing is not contributing
        self.assertEqual(
            pub.post(f"/api/specs/{spec['id']}/flags", {"reason": "incorrect"})[0], 200)
        mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": None})

    def test_2h_fixed_blocks_community_values_but_not_the_manager(self):
        mgr = self.as_("m.alvarez")
        pub = self.as_("t.moreno")
        gap = self._empty_spec(pub)
        mgr.patch(f"/api/specs/{gap['id']}/type", {"spec_type": "fixed"})

        s, b = pub.post(f"/api/specs/{gap['id']}/value", {"value": "drive-by"})
        self.assertEqual(s, 403)
        self.assertIn("marked Fixed", b["error"])

        # the manager is not locked out by their own marker
        s, _ = mgr.post(f"/api/specs/{gap['id']}/value", {"value": "the right answer"})
        self.assertEqual(s, 200)

    def test_2i_the_tag_is_per_bike_not_platform_wide(self):
        """A manager marking one spec Fixed must not change that field for the
        other 259 bikes — that is admin's authority, not theirs."""
        mgr = self.as_("m.alvarez")
        spec = self._open_spec(mgr)
        mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": "fixed"})

        con = sqlite3.connect(self.db)
        field_default = con.execute(
            "SELECT spec_type FROM spec_fields WHERE field_key=?",
            (spec["field_key"],)).fetchone()[0]
        overrides = con.execute(
            "SELECT COUNT(*) FROM specs WHERE field_key=? AND spec_type IS NOT NULL",
            (spec["field_key"],)).fetchone()[0]
        con.close()
        self.assertEqual(field_default, spec["field_spec_type"],
                         "the platform-wide field default was changed")
        self.assertEqual(overrides, 1, "the override leaked to other bikes")
        mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": None})

    def test_2j_only_the_bikes_manager_can_set_the_tag(self):
        pub = self.as_("t.moreno")
        spec = self._open_spec(pub)
        self.assertEqual(
            pub.patch(f"/api/specs/{spec['id']}/type", {"spec_type": "fixed"})[0], 403)
        self.assertEqual(
            self.anon().patch(f"/api/specs/{spec['id']}/type", {"spec_type": "fixed"})[0], 401)

    def test_2k_bad_type_refused(self):
        mgr = self.as_("m.alvarez")
        spec = self._open_spec(mgr)
        self.assertEqual(
            mgr.patch(f"/api/specs/{spec['id']}/type", {"spec_type": "nonsense"})[0], 400)
        s, b = mgr.patch(f"/api/specs/{spec['id']}/type", {})
        self.assertEqual(s, 400)
        self.assertIn("required", b["error"])

    def test_31_alternates_refused_on_fixed_fields(self):
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for cat in specs["categories"] for sp in cat["specs"]]
        fixed = next(sp for sp in flat if sp["spec_type"] == "fixed")
        s, b = c.post(f"/api/specs/{fixed['id']}/alternates", {"text": "made up"})
        self.assertEqual(s, 400)
        self.assertIn("single correct value", b["error"])

    def test_32_alternate_allowed_on_pref_field(self):
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for cat in specs["categories"] for sp in cat["specs"]]
        pref = next(sp for sp in flat if sp["spec_type"] in ("pref", "community"))
        s, _ = c.post(f"/api/specs/{pref['id']}/alternates", {"text": "test alt"})
        self.assertEqual(s, 200)

    def test_33_flag_detail_length_enforced(self):
        c = self.as_("t.moreno")
        _, specs = c.get(f"/api/bikes/{self.cb919}/specs")
        spec = specs["categories"][0]["specs"][0]
        s, _ = c.post(f"/api/specs/{spec['id']}/flags",
                      {"reason": "incorrect", "detail": "x" * 151})
        self.assertEqual(s, 400)

    def test_34_cannot_withdraw_someone_elses_flag(self):
        flagger = self.as_("t.moreno")
        _, specs = flagger.get(f"/api/bikes/{self.cb919}/specs")
        # A value this user has not already flagged — one open flag per person
        # per value, so re-flagging would 409 instead of returning a new id.
        spec = next(sp for cat in specs["categories"] for sp in cat["specs"]
                    if sp["value"] and not sp["my_flag"])
        _, made = flagger.post(f"/api/specs/{spec['id']}/flags",
                               {"reason": "other", "detail": "mine"})
        self.assertIn("id", made, made)
        s, _ = self.as_("rider_kestrel99").delete(f"/api/flags/{made['id']}")
        self.assertEqual(s, 403)
        self.assertEqual(flagger.delete(f"/api/flags/{made['id']}")[0], 200)

    def test_35_cannot_flag_yourself(self):
        c = self.as_("t.moreno")
        _, me = c.get("/api/auth/me")
        s, _ = c.post(f"/api/users/{me['user']['id']}/flags", {"reason": "spam"})
        self.assertEqual(s, 400)

    # -- manager workflow -------------------------------------------------
    def test_40_manager_sees_only_their_queue(self):
        s, b = self.as_("m.alvarez").get("/api/manager/flags")
        self.assertEqual(s, 200)
        self.assertGreaterEqual(len(b["flags"]), 3)
        for f in b["flags"]:
            self.assertEqual(f["bike_id"], self.cb919)

    def test_41_fix_writes_value_and_logs_the_change(self):
        mgr = self.as_("m.alvarez")
        _, q = mgr.get("/api/manager/flags")
        flag = next(f for f in q["flags"] if f["spec_label"] == "Coolant Capacity")
        old = flag["current_value"]

        s, _ = mgr.post(f"/api/flags/{flag['id']}/fix", {"new_value": "2.9 L (total system)"})
        self.assertEqual(s, 200)

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        self.assertEqual(
            next(sp for sp in flat if sp["label"] == "Coolant Capacity")["value"],
            "2.9 L (total system)")

        _, res = mgr.get("/api/manager/resolved")
        entry = next(r for r in res["resolved"] if r["spec_label"] == "Coolant Capacity")
        self.assertEqual(entry["outcome"], "fixed")
        self.assertEqual(entry["old_value"], old)
        self.assertEqual(entry["new_value"], "2.9 L (total system)")

    def test_42_fixing_twice_is_refused(self):
        mgr = self.as_("m.alvarez")
        _, q = mgr.get("/api/manager/flags")
        # Fix Value only applies to the stock value; a flag against an
        # alternate is refused by design, so pick one it can act on.
        flag = next(f for f in q["flags"] if not f["alternate_id"])
        self.assertEqual(mgr.post(f"/api/flags/{flag['id']}/fix", {"new_value": "a"})[0], 200)
        s, b = mgr.post(f"/api/flags/{flag['id']}/fix", {"new_value": "b"})
        self.assertEqual(s, 400)
        self.assertIn("already resolved", b["error"])

    def test_43_dismiss_keeps_value_and_is_marked_dismissed(self):
        mgr = self.as_("m.alvarez")
        _, q = mgr.get("/api/manager/flags")
        flag = next(f for f in q["flags"] if not f["alternate_id"])
        before = flag["current_value"]
        self.assertEqual(mgr.post(f"/api/flags/{flag['id']}/dismiss")[0], 200)

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        flat = [sp for c in specs["categories"] for sp in c["specs"]]
        after = next(sp for sp in flat if sp["id"] == flag["spec_id"])["value"]
        self.assertEqual(after, before, "dismiss must not change the value")

        _, res = mgr.get("/api/manager/resolved")
        self.assertEqual(
            next(r for r in res["resolved"] if r["id"] == flag["id"])["outcome"],
            "dismissed")

    def test_44_proposal_round_trip(self):
        mgr = self.as_("m.alvarez")
        s, made = mgr.post("/api/proposals", {
            "field_name": "Steering Damper Preload", "category": "Suspension",
            "bike_id": self.cb919, "reasoning": "no field exists for it"})
        self.assertEqual(s, 200)

        adm = self.as_("admin")
        _, pending = adm.get("/api/admin/proposals")
        self.assertIn(made["id"], [p["id"] for p in pending["items"]])

        s, out = adm.post(f"/api/admin/proposals/{made['id']}/decide",
                          {"status": "approved", "apply_to_bike": True})
        self.assertEqual(s, 200)
        self.assertEqual(out["created_field_key"], "steering_damper_preload")

        # Approved means it exists for real, on the tree and on the bike.
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertIn("Steering Damper Preload", labels)

    def test_44b_proposal_queue_carries_the_fields_the_console_needs(self):
        """The admin console offers 'add this field to the proposing bike now',
        which needs bike_id in the payload. Selecting only bike_name made the
        checkbox silently vanish and the option was never actually offered —
        the approval still succeeded, so nothing failed loudly."""
        mgr = self.as_("m.alvarez")
        _, made = mgr.post("/api/proposals", {
            "field_name": "Fork Oil Weight Test", "category": "Suspension",
            "bike_id": self.cb919, "reasoning": "needs a bike id in the queue"})
        _, pending = self.as_("admin").get("/api/admin/proposals")
        row = next(p for p in pending["items"] if p["id"] == made["id"])
        self.assertEqual(row["bike_id"], self.cb919)
        self.assertIn("bike_name", row)

    # -- approving a proposal into the tree -------------------------------
    def test_44c_branch_list_shows_answer_counts(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "BRANCHLIST TEST", 1970, 1970)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})

        s, b = adm.get("/api/admin/branches")
        self.assertEqual(s, 200)
        q3 = next(x for x in b["branches"] if x["question_id"] == "q3")
        chain = next(o for o in q3["options"] if o["label"] == "A")
        self.assertGreaterEqual(chain["bikes"], 1,
                                "a bike that answered q3=A is not counted")
        self.assertGreaterEqual(b["bikes_with_answers"], 1)
        self.assertGreater(b["bikes_total"], b["bikes_with_answers"],
                           "catalog bikes should have no answers on record")

    def test_44d_approved_field_attached_to_a_branch_reaches_future_bikes(self):
        """The hole this closes: an approved field used to be invisible to the
        questionnaire, so no bike built afterwards ever received it."""
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")
        _, made = mgr.post("/api/proposals", {
            "field_name": "Master Link Clip Orientation", "category": "Drive",
            "reasoning": "chain-drive bikes only"})

        s, r = adm.post(f"/api/admin/proposals/{made['id']}/decide", {
            "status": "approved", "trigger_question": "q3", "trigger_option": "A"})
        self.assertEqual(s, 200, r)
        key = r["created_field_key"]

        # a NEW bike answering q3=A must now get it
        bike = self._new_bike(adm, "TRIGGER TEST", 1971, 1971)
        _, built = adm.post(f"/api/questionnaire/{bike}/build",
                            {"answers": self.CB750_ANSWERS})
        self.assertGreaterEqual(built["from_approved_branches"], 1)
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertIn("Master Link Clip Orientation", labels)

        # ...and a bike that answers differently must not
        shaft = dict(self.CB750_ANSWERS, q3="C")
        other = self._new_bike(adm, "NOTRIGGER TEST", 1972, 1972)
        adm.post(f"/api/questionnaire/{other}/build", {"answers": shaft})
        _, specs2 = adm.get(f"/api/bikes/{other}/specs")
        labels2 = [sp["label"] for c in specs2["categories"] for sp in c["specs"]]
        self.assertNotIn("Master Link Clip Orientation", labels2)

    def test_44e_backfill_is_optional_and_reported(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        bike = self._new_bike(adm, "BACKFILL TEST", 1973, 1973)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})

        # without backfill the existing bike is untouched
        _, p1 = mgr.post("/api/proposals", {"field_name": "Chain Guard Bolt Torque",
                                            "category": "Drive", "reasoning": "x"})
        _, r1 = adm.post(f"/api/admin/proposals/{p1['id']}/decide", {
            "status": "approved", "trigger_question": "q3", "trigger_option": "A"})
        self.assertEqual(r1["backfilled"], 0)
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        self.assertNotIn("Chain Guard Bolt Torque",
                         [sp["label"] for c in specs["categories"] for sp in c["specs"]])

        # with backfill it lands on every bike that answered that way
        _, p2 = mgr.post("/api/proposals", {"field_name": "Chain Slack Check Interval",
                                            "category": "Drive", "reasoning": "x"})
        _, r2 = adm.post(f"/api/admin/proposals/{p2['id']}/decide", {
            "status": "approved", "trigger_question": "q3", "trigger_option": "A",
            "backfill": True})
        self.assertGreaterEqual(r2["backfilled"], 1)
        _, specs2 = adm.get(f"/api/bikes/{bike}/specs")
        got = next(sp for c in specs2["categories"] for sp in c["specs"]
                   if sp["label"] == "Chain Slack Check Interval")
        self.assertIsNone(got["value"], "back-filled fields must start empty")

    def test_44f_backfill_skips_bikes_that_never_answered(self):
        """Catalog bikes answered nothing, so no branch reaches them — a
        spreadsheet row is not an answer somebody gave."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Chain Lube Brand Pref",
                                           "category": "Drive", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "trigger_question": "q3", "trigger_option": "A",
            "backfill": True})
        # bike 1 is a bulk-catalog import with no questionnaire answers
        _, specs = adm.get("/api/bikes/1/specs")
        self.assertNotIn("Chain Lube Brand Pref",
                         [sp["label"] for c in specs["categories"] for sp in c["specs"]])

    def test_44g_invalid_branch_refused(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        for body, expect in [
            ({"trigger_question": "q3"}, "needs both"),
            ({"trigger_question": "q999", "trigger_option": "A"}, "no such question"),
            ({"trigger_question": "q3", "trigger_option": "Z"}, "not an option"),
        ]:
            _, p = mgr.post("/api/proposals", {
                "field_name": f"Bad Branch {expect}", "category": "Drive",
                "reasoning": "x"})
            s, b = adm.post(f"/api/admin/proposals/{p['id']}/decide",
                            {"status": "approved", **body})
            self.assertEqual(s, 400, b)
            self.assertIn(expect, b["error"])

    def test_44h_answers_are_recorded_for_the_bike(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "ANSWERS TEST", 1974, 1974)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})
        con = sqlite3.connect(self.db)
        rows = dict(con.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?",
            (bike,)).fetchall())
        con.close()
        self.assertEqual(rows.get("q3"), "A")
        self.assertEqual(rows.get("q0"), "C")
        # re-running replaces rather than accumulating
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": dict(self.CB750_ANSWERS, q3="C")})
        con = sqlite3.connect(self.db)
        again = dict(con.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?",
            (bike,)).fetchall())
        con.close()
        self.assertEqual(again.get("q3"), "C")

    def test_44i_approving_with_no_destination_is_surfaced(self):
        """Three real approvals created fields that reached no bike and nothing
        said so. The field is still created — that is the admin's call — but it
        has to be findable afterwards."""
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")
        _, p = mgr.post("/api/proposals", {"field_name": "Orphan Field Test",
                                           "category": "General", "reasoning": "x"})
        adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})

        _, un = adm.get("/api/admin/unplaced-fields")
        row = next((f for f in un["fields"] if f["label"] == "Orphan Field Test"), None)
        self.assertIsNotNone(row, "an unplaced field is not reported anywhere")
        self.assertEqual(row["triggers"], 0)

    def test_44j_a_branch_nobody_matches_still_counts_as_unplaced(self):
        """Attached to a branch, but still on no bike — what happened to "belt
        conditioner" in practice. Uses belt drive (q3=B): no bike in the suite
        answers it, exactly as no real bike had answered driveshaft."""
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")
        # Any branch nobody has answered. Hardcoding one is fragile: another
        # test only has to build a bike that answers that way and the premise
        # silently stops holding.
        _, branches = adm.get("/api/admin/branches")
        qid, opt = next((b["question_id"], o["label"])
                        for b in branches["branches"]
                        for o in b["options"] if o["bikes"] == 0)

        _, p = mgr.post("/api/proposals", {"field_name": "Belt Only Test",
                                           "category": "Drive", "reasoning": "x"})
        adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "trigger_question": qid, "trigger_option": opt,
            "backfill": True})
        _, un = adm.get("/api/admin/unplaced-fields")
        row = next((f for f in un["fields"] if f["label"] == "Belt Only Test"), None)
        self.assertIsNotNone(row, "a field attached to an unmatched branch is hidden")
        self.assertEqual(row["triggers"], 1)
        self.assertIn(f"{qid}={opt}", row["trigger_list"])

    def test_44b2_admin_can_reword_the_field_on_approval(self):
        """A proposer types "belt conditioner"; what lands on the tree is read
        by every rider on every bike, so the admin settles the wording."""
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")
        _, p = mgr.post("/api/proposals", {"field_name": "belt conditioner stuff",
                                           "category": "Drive", "reasoning": "x"})
        s, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "field_name": "Drive Belt Conditioner"})
        self.assertEqual(s, 200, r)
        self.assertTrue(r["renamed"])
        self.assertEqual(r["created_label"], "Drive Belt Conditioner")
        self.assertEqual(r["created_field_key"], "drive_belt_conditioner")

        con = sqlite3.connect(self.db)
        label = con.execute("SELECT label FROM spec_fields WHERE field_key=?",
                            (r["created_field_key"],)).fetchone()[0]
        # the proposal keeps what was actually proposed, so the change is visible
        proposed = con.execute("SELECT field_name FROM branch_proposals WHERE id=?",
                               (p["id"],)).fetchone()[0]
        con.close()
        self.assertEqual(label, "Drive Belt Conditioner")
        self.assertEqual(proposed, "belt conditioner stuff")

    def test_44b3_omitting_the_name_keeps_the_proposed_one(self):
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")
        _, p = mgr.post("/api/proposals", {"field_name": "Kept As Proposed",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        self.assertFalse(r["renamed"])
        self.assertEqual(r["created_label"], "Kept As Proposed")

    def test_44b4_reworded_name_is_validated(self):
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")

        _, p1 = mgr.post("/api/proposals", {"field_name": "Blank Name Test",
                                            "category": "General", "reasoning": "x"})
        s, b = adm.post(f"/api/admin/proposals/{p1['id']}/decide",
                        {"status": "approved", "field_name": "   "})
        self.assertEqual(s, 400)
        self.assertIn("cannot be empty", b["error"])

        _, p2 = mgr.post("/api/proposals", {"field_name": "Punctuation Test",
                                            "category": "General", "reasoning": "x"})
        s, b = adm.post(f"/api/admin/proposals/{p2['id']}/decide",
                        {"status": "approved", "field_name": "!!! ???"})
        self.assertEqual(s, 400)
        self.assertIn("letters or numbers", b["error"])

        # renaming onto a field that already exists is still refused
        _, p3 = mgr.post("/api/proposals", {"field_name": "Collide Test",
                                            "category": "General", "reasoning": "x"})
        s, b = adm.post(f"/api/admin/proposals/{p3['id']}/decide",
                        {"status": "approved", "field_name": "Seat Height"})
        self.assertEqual(s, 409)
        self.assertIn("already exists", b["error"])

    def test_44j2_a_mispicked_branch_can_be_corrected(self):
        """belt conditioner was approved onto Driveshaft when it belongs to
        Belt drive, and approval was the only place a branch could be set —
        so the mistake was uncorrectable. It has to be changeable."""
        mgr = self.as_("m.alvarez")
        adm = self.as_("admin")
        _, p = mgr.post("/api/proposals", {"field_name": "Belt Dressing Test",
                                           "category": "Drive", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "trigger_question": "q3", "trigger_option": "C"})
        key = r["created_field_key"]

        s, b = adm.post(f"/api/admin/fields/{key}/trigger", {
            "trigger_question": "q3", "trigger_option": "B"})
        self.assertEqual(s, 200, b)
        self.assertEqual(b["triggers"], [{"question": "q3", "option": "B"}])
        self.assertEqual(b["option_text"], "Belt drive")

        # replaced, not added — one question cannot mean two answers
        con = sqlite3.connect(self.db)
        trig = con.execute(
            "SELECT question_id, option_label FROM field_triggers WHERE field_key=?",
            (key,)).fetchall()
        con.close()
        self.assertEqual(trig, [("q3", "B")])

        # and the corrected branch now feeds new bikes
        bike = self._new_bike(adm, "BELTBIKE TEST", 1985, 1985)
        belt_answers = dict(self.CB750_ANSWERS, q3="B")
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": belt_answers})
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertIn("Belt Dressing Test", labels)
        self.assertIn("Drive Belt", labels, "the built-in belt field should come too")
        self.assertNotIn("Drive Shaft Oil Weight", labels)

    def test_44j2b_a_field_can_belong_to_several_branches(self):
        """Chain OR belt, but not shaft. Two answers to one question are a
        union, not a contradiction — which is why several branches are allowed
        on one field."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Final Drive Tension",
                                           "category": "Drive", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved",
            "triggers": [{"question": "q3", "option": "A"},
                         {"question": "q3", "option": "B"}]})
        key = r["created_field_key"]

        con = sqlite3.connect(self.db)
        trig = sorted(con.execute(
            "SELECT question_id, option_label FROM field_triggers WHERE field_key=?",
            (key,)).fetchall())
        con.close()
        self.assertEqual(trig, [("q3", "A"), ("q3", "B")])

        # chain bike gets it, belt bike gets it, shaft bike does not
        for opt, expect in (("A", True), ("B", True), ("C", False)):
            bike = self._new_bike(adm, f"MULTI {opt} TEST", 1940 + ord(opt), 1940 + ord(opt))
            adm.post(f"/api/questionnaire/{bike}/build",
                     {"answers": dict(self.CB750_ANSWERS, q3=opt)})
            _, specs = adm.get(f"/api/bikes/{bike}/specs")
            labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
            self.assertEqual("Final Drive Tension" in labels, expect,
                             f"q3={opt} should{'' if expect else ' not'} receive it")

    def test_44j2c_replacing_branches_removes_the_old_ones(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Replace Branches Test",
                                           "category": "Drive", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved",
            "triggers": [{"question": "q3", "option": "A"},
                         {"question": "q3", "option": "C"}]})
        key = r["created_field_key"]

        # narrow it to belt only — the dialog's list is what gets stored
        _, b = adm.post(f"/api/admin/fields/{key}/trigger", {
            "triggers": [{"question": "q3", "option": "B"}]})
        self.assertEqual(b["triggers"], [{"question": "q3", "option": "B"}])

        con = sqlite3.connect(self.db)
        trig = con.execute(
            "SELECT question_id, option_label FROM field_triggers WHERE field_key=?",
            (key,)).fetchall()
        con.close()
        self.assertEqual(trig, [("q3", "B")])

    def test_44j2d_multi_branch_backfill_counts_each_bike_once(self):
        """A bike matching two of the chosen branches must not be counted or
        written twice."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Overlap Backfill Test",
                                           "category": "Drive", "reasoning": "x"})
        # q0=C and q3=A both hold for the standard answer set
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "backfill": True,
            "triggers": [{"question": "q0", "option": "C"},
                         {"question": "q3", "option": "A"}]})
        key = r["created_field_key"]

        con = sqlite3.connect(self.db)
        rows_written = con.execute(
            "SELECT COUNT(*) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
        distinct_bikes = con.execute(
            "SELECT COUNT(DISTINCT bike_id) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
        con.close()
        self.assertEqual(rows_written, distinct_bikes, "a bike got the field twice")
        self.assertEqual(r["backfilled"], rows_written)

    def test_44j3_changing_a_branch_validates_and_can_be_cleared(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Branch Validate Test",
                                           "category": "Drive", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]

        self.assertEqual(adm.post(f"/api/admin/fields/{key}/trigger",
                                  {"trigger_question": "q999", "trigger_option": "A"})[0], 400)
        self.assertEqual(adm.post(f"/api/admin/fields/{key}/trigger",
                                  {"trigger_question": "q3", "trigger_option": "Z"})[0], 400)
        self.assertEqual(adm.post("/api/admin/fields/not_a_field/trigger",
                                  {"trigger_question": "q3", "trigger_option": "B"})[0], 404)

        adm.post(f"/api/admin/fields/{key}/trigger",
                 {"trigger_question": "q3", "trigger_option": "B"})
        _, cleared = adm.delete(f"/api/admin/fields/{key}/trigger")
        self.assertEqual(cleared["removed"], 1)

    def test_44j4_a_field_can_apply_to_every_bike(self):
        """Some specs are not conditional on anything — road trip tools belong
        on every machine. The point is that bikes created later are covered
        too, without anyone re-running a back-fill."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Road Kit Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]

        con = sqlite3.connect(self.db)
        total = con.execute("SELECT COUNT(*) FROM bikes").fetchone()[0]
        con.close()

        s, b = adm.post(f"/api/admin/fields/{key}/universal",
                        {"universal": True, "backfill": True})
        self.assertEqual(s, 200, b)
        self.assertTrue(b["universal"])
        self.assertEqual(b["added"], total, "back-fill missed some bikes")

        # on an existing bike...
        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        got = next(sp for c in specs["categories"] for sp in c["specs"]
                   if sp["label"] == "Road Kit Test")
        self.assertIsNone(got["value"], "a universal field must start empty")

        # ...on a bulk-catalog bike, which answers no questionnaire...
        _, cat = adm.get("/api/bikes/1/specs")
        self.assertIn("Road Kit Test",
                      [sp["label"] for c in cat["categories"] for sp in c["specs"]])

        # ...and on a bike created afterwards, with no back-fill re-run
        new_bike = self._new_bike(adm, "UNIVERSAL AFTER TEST", 1966, 1966)
        _, fresh = adm.get(f"/api/bikes/{new_bike}/specs")
        self.assertIn("Road Kit Test",
                      [sp["label"] for c in fresh["categories"] for sp in c["specs"]],
                      "a bike created later did not receive the universal field")

        # it is no longer reported as unplaced
        _, un = adm.get("/api/admin/unplaced-fields")
        self.assertNotIn("Road Kit Test", [f["label"] for f in un["fields"]])

    def test_44j4b_universal_is_refused_on_a_branch_restricted_field(self):
        """A 2-stroke oil spec restricted to q5=A was marked "every bike" and
        landed on 260 four-strokes. "Every bike" and "only bikes answering q5=A"
        contradict each other, and universal silently won."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Two Stroke Only Test",
                                           "category": "Engine", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "trigger_question": "q5", "trigger_option": "A"})
        key = r["created_field_key"]

        s, b = adm.post(f"/api/admin/fields/{key}/universal",
                        {"universal": True, "backfill": True})
        self.assertEqual(s, 409)
        self.assertIn("contradicts", b["error"])
        self.assertIn("q5=A", b["error"])

        con = sqlite3.connect(self.db)
        universal, rows_written = con.execute(
            "SELECT (SELECT universal FROM spec_fields WHERE field_key=?),"
            " (SELECT COUNT(*) FROM specs WHERE field_key=?)", (key, key)).fetchone()
        con.close()
        self.assertEqual(universal, 0, "the flag was set despite the refusal")
        self.assertEqual(rows_written, 0, "bikes were written despite the refusal")

        # clearing the branch first makes it legitimate
        adm.delete(f"/api/admin/fields/{key}/trigger")
        s, b = adm.post(f"/api/admin/fields/{key}/universal",
                        {"universal": True, "backfill": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["universal"])

    def test_44j5_turning_universal_off_keeps_existing_rows(self):
        """Un-marking must not delete spec rows — they can hold values somebody
        entered, and silently destroying those is the one irreversible act in
        this flow."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Toggle Off Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]
        adm.post(f"/api/admin/fields/{key}/universal", {"universal": True, "backfill": True})

        con = sqlite3.connect(self.db)
        before = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
        con.close()
        self.assertGreater(before, 0)

        _, off = adm.post(f"/api/admin/fields/{key}/universal", {"universal": False})
        self.assertFalse(off["universal"])

        con = sqlite3.connect(self.db)
        after = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
        con.close()
        self.assertEqual(after, before, "existing spec rows were destroyed")

        # but a bike created now no longer receives it
        fresh_bike = self._new_bike(adm, "AFTER UNSET TEST", 1967, 1967)
        _, specs = adm.get(f"/api/bikes/{fresh_bike}/specs")
        self.assertNotIn("Toggle Off Test",
                         [sp["label"] for c in specs["categories"] for sp in c["specs"]])

    def test_44j6_universal_flag_validates(self):
        adm = self.as_("admin")
        self.assertEqual(adm.post("/api/admin/fields/nope_field/universal",
                                  {"universal": True})[0], 404)
        _, un = adm.get("/api/admin/unplaced-fields")
        if un["fields"]:
            key = un["fields"][0]["field_key"]
            s, b = adm.post(f"/api/admin/fields/{key}/universal", {"universal": "yes"})
            self.assertEqual(s, 400)
            self.assertIn("true or false", b["error"])

    def test_44k_admin_can_place_a_field_on_named_bikes(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Place Me Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]

        s, b = adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [self.cb919]})
        self.assertEqual(s, 200)
        self.assertEqual(b["added"], 1)

        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        got = next(sp for c in specs["categories"] for sp in c["specs"]
                   if sp["label"] == "Place Me Test")
        self.assertIsNone(got["value"], "a placed field must start empty")

        # it is no longer unplaced, and re-applying is a no-op
        _, un = adm.get("/api/admin/unplaced-fields")
        self.assertNotIn("Place Me Test", [f["label"] for f in un["fields"]])
        _, again = adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [self.cb919]})
        self.assertEqual(again["added"], 0)

    def test_44l_placing_validates_input(self):
        adm = self.as_("admin")
        self.assertEqual(adm.post("/api/admin/fields/nope_not_a_field/apply",
                                  {"bike_ids": [self.cb919]})[0], 404)
        _, un = adm.get("/api/admin/unplaced-fields")
        if un["fields"]:
            key = un["fields"][0]["field_key"]
            self.assertEqual(adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": []})[0], 400)
            s, b = adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [999999]})
            self.assertEqual(s, 404)
            self.assertIn("no such bike", b["error"])

    # -- removing branches and specs --------------------------------------
    # -- the admin activity log -------------------------------------------
    def test_44s_actions_are_recorded_with_a_readable_summary(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, before = adm.get("/api/admin/activity")

        _, p = mgr.post("/api/proposals", {"field_name": "Audit Trail Test",
                                           "category": "General", "reasoning": "x"})
        adm.post(f"/api/admin/proposals/{p['id']}/decide",
                 {"status": "approved", "field_name": "Audit Trail Renamed"})

        _, after = adm.get("/api/admin/activity")
        self.assertGreater(after["total"], before["total"])
        top = after["actions"][0]
        self.assertEqual(top["action"], "proposal.approve")
        self.assertIn("Audit Trail Test", top["summary"])
        self.assertIn("Audit Trail Renamed", top["summary"])
        self.assertEqual(top["username"], "admin")

    def test_44t_a_destructive_delete_records_what_it_destroyed(self):
        """The only remaining record of a deleted value is this log entry."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Lost Values Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]
        adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [self.cb919]})
        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        spec = next(sp for c in specs["categories"] for sp in c["specs"]
                    if sp["label"] == "Lost Values Test")
        adm.patch(f"/api/specs/{spec['id']}", {"value": "irreplaceable"})

        adm.delete(f"/api/admin/fields/{key}", {"force": True})

        _, act = adm.get("/api/admin/activity?destructive=1")
        entry = next(a for a in act["actions"] if a["target"] == key)
        self.assertTrue(entry["destructive"])
        detail = json.loads(entry["detail"])
        self.assertEqual(detail["values_destroyed"], 1)
        self.assertEqual(detail["lost_values"][0]["value"], "irreplaceable")
        self.assertEqual(detail["lost_values"][0]["bike_id"], self.cb919)

    def test_44u_activity_defaults_to_mine_and_can_widen(self):
        adm = self.as_("admin")
        _, mine = adm.get("/api/admin/activity")
        self.assertTrue(all(a["username"] == "admin" for a in mine["actions"]))
        _, everyone = adm.get("/api/admin/activity?who=all")
        self.assertGreaterEqual(everyone["total"], mine["total"])

    def test_44v_a_failed_action_leaves_no_log_entry(self):
        """The log row is written on the same connection as the change, so a
        refused action must not appear as though it happened."""
        adm = self.as_("admin")
        _, before = adm.get("/api/admin/activity?who=all")

        # refused: the field is in use and force was not given
        _, fields = adm.get("/api/admin/fields?q=Seat")
        used = next(f for f in fields["fields"] if f["bikes"] > 0)
        self.assertEqual(adm.delete(f"/api/admin/fields/{used['field_key']}")[0], 409)

        _, after = adm.get("/api/admin/activity?who=all")
        self.assertEqual(after["total"], before["total"],
                         "a refused delete was logged as if it had happened")

    # -- field type, tree-wide ---------------------------------------------
    def test_44v2_fixed_is_off_by_default(self):
        """111 of 120 fields carried 'fixed' because it was the schema default,
        not because anyone judged them — and 'fixed' refuses alternates, so the
        default quietly closed contribution across the whole tree."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Default Type Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})

        con = sqlite3.connect(self.db)
        got = con.execute("SELECT spec_type FROM spec_fields WHERE field_key=?",
                          (r["created_field_key"],)).fetchone()[0]
        fixed = con.execute(
            "SELECT COUNT(*) FROM spec_fields WHERE spec_type='fixed'").fetchone()[0]
        total = con.execute("SELECT COUNT(*) FROM spec_fields").fetchone()[0]
        con.close()
        self.assertEqual(got, "pref", "a new field defaulted to fixed")
        self.assertLess(fixed, total / 2, "most of the tree is still fixed")

    def test_44v3_admin_can_change_a_field_type_tree_wide(self):
        adm = self.as_("admin")
        spec = self._open_spec(adm)
        key = spec["field_key"]

        s, b = adm.patch(f"/api/admin/fields/{key}/type", {"spec_type": "fixed"})
        self.assertEqual(s, 200, b)
        self.assertEqual(b["spec_type"], "fixed")
        self.assertEqual(b["was"], "pref")

        # it now refuses alternates on every bike carrying the field
        pub = self.as_("t.moreno")
        _, specs = pub.get(f"/api/bikes/{self.cb919}/specs")
        after = next(sp for c in specs["categories"] for sp in c["specs"]
                     if sp["field_key"] == key)
        self.assertEqual(after["spec_type"], "fixed")
        self.assertEqual(
            pub.post(f"/api/specs/{after['id']}/alternates", {"text": "nope"})[0], 400)

        # and taking it away reopens them
        adm.patch(f"/api/admin/fields/{key}/type", {"spec_type": "pref"})
        _, again = pub.get(f"/api/bikes/{self.cb919}/specs")
        reopened = next(sp for c in again["categories"] for sp in c["specs"]
                        if sp["field_key"] == key)
        self.assertEqual(reopened["spec_type"], "pref")
        self.assertEqual(
            pub.post(f"/api/specs/{reopened['id']}/alternates",
                     {"text": "allowed now"})[0], 200)

    def test_44v4_field_type_validates_and_is_logged(self):
        adm = self.as_("admin")
        spec = self._open_spec(adm)
        self.assertEqual(adm.patch(f"/api/admin/fields/{spec['field_key']}/type",
                                   {"spec_type": "nonsense"})[0], 400)
        self.assertEqual(adm.patch("/api/admin/fields/not_a_field/type",
                                   {"spec_type": "fixed"})[0], 404)

        adm.patch(f"/api/admin/fields/{spec['field_key']}/type",
                  {"spec_type": "community"})
        _, act = adm.get("/api/admin/activity")
        entry = next(a for a in act["actions"] if a["action"] == "field.type")
        self.assertIn("community", entry["summary"])
        adm.patch(f"/api/admin/fields/{spec['field_key']}/type", {"spec_type": "pref"})

    # -- display order -----------------------------------------------------
    def test_44w_specs_display_in_the_curated_order_not_alphabetically(self):
        """The CB919 sheet lists its Engine specs in a deliberate reading
        order. Alphabetical split the two coolant entries apart and sorted
        "Spark Plug Gap" above "Spark Plug — Standard"."""
        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        engine = next(c for c in specs["categories"] if c["name"] == "Engine")
        labels = [sp["label"] for sp in engine["specs"]]

        self.assertLess(labels.index("Cylinder Configuration"),
                        labels.index("Bore × Stroke"))
        self.assertLess(labels.index("Spark Plug — Standard"),
                        labels.index("Spark Plug Gap"))
        self.assertEqual(labels.index("Coolant Capacity"),
                         labels.index("Coolant Type") + 1,
                         "the coolant specs should sit together")
        self.assertNotEqual(labels, sorted(labels),
                            "still falling back to alphabetical")

    def test_44x_each_category_renders_once(self):
        """Category headings are built from consecutive runs, so a field whose
        sort_order strayed into another band would produce a second heading."""
        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        names = [c["name"] for c in specs["categories"]]
        self.assertEqual(len(names), len(set(names)), f"duplicate heading: {names}")

    def test_44y_admin_can_reorder_a_field(self):
        adm = self.as_("admin")
        _, before = adm.get("/api/admin/fields?q=Coolant")
        pair = sorted([f for f in before["fields"] if f["category"] == "Engine"],
                      key=lambda f: f["sort_order"])[:2]
        first, second = pair[0], pair[1]
        self.assertLess(first["sort_order"], second["sort_order"])
        # the endpoint should already return them in that order
        listed = [f["sort_order"] for f in before["fields"]]
        self.assertEqual(listed, sorted(listed), "field list is not in sort_order")

        s, b = adm.post(f"/api/admin/fields/{second['field_key']}/move",
                        {"direction": "up"})
        self.assertEqual(s, 200, b)
        self.assertEqual(b["swapped_with"], first["label"])

        _, after = adm.get("/api/admin/fields?q=Coolant")
        moved = next(f for f in after["fields"] if f["field_key"] == second["field_key"])
        stayed = next(f for f in after["fields"] if f["field_key"] == first["field_key"])
        self.assertEqual(moved["sort_order"], first["sort_order"])
        self.assertEqual(stayed["sort_order"], second["sort_order"])

        # the page reflects it
        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        engine = next(c for c in specs["categories"] if c["name"] == "Engine")
        labels = [sp["label"] for sp in engine["specs"]]
        if second["label"] in labels and first["label"] in labels:
            self.assertLess(labels.index(second["label"]), labels.index(first["label"]))

        adm.post(f"/api/admin/fields/{second['field_key']}/move", {"direction": "down"})

    def test_44z_moving_past_the_end_is_refused(self):
        adm = self.as_("admin")
        _, fields = adm.get("/api/admin/fields?q=Engine&limit=200")
        engine = sorted([f for f in fields["fields"] if f["category"] == "Engine"],
                        key=lambda f: f["sort_order"])
        if not engine:
            self.skipTest("no Engine fields matched")
        s, b = adm.post(f"/api/admin/fields/{engine[0]['field_key']}/move",
                        {"direction": "up"})
        self.assertEqual(s, 409)
        self.assertIn("already first", b["error"])
        self.assertEqual(adm.post(f"/api/admin/fields/{engine[0]['field_key']}/move",
                                  {"direction": "sideways"})[0], 400)

    def test_44za_reordering_stays_inside_the_category(self):
        """Sort order also decides the category band. A field crossing one
        would appear under another category's heading."""
        adm = self.as_("admin")
        _, fields = adm.get("/api/admin/fields?q=a&limit=200")
        by_cat = {}
        for f in fields["fields"]:
            by_cat.setdefault(f["category"], []).append(f)
        for cat, items in by_cat.items():
            items.sort(key=lambda f: f["sort_order"])
            last = items[-1]
            if last["position"] != last["siblings"] - 1:
                continue          # search did not return the whole category
            s, _ = adm.post(f"/api/admin/fields/{last['field_key']}/move",
                            {"direction": "down"})
            self.assertEqual(s, 409, f"{cat} let its last field move out")
            break

    def test_44n_field_list_reports_what_removal_would_cost(self):
        adm = self.as_("admin")
        s, b = adm.get("/api/admin/fields?q=Seat")
        self.assertEqual(s, 200)
        row = next(f for f in b["fields"] if f["label"] == "Seat Height")
        self.assertGreater(row["bikes"], 0)
        self.assertGreater(row["values_set"], 0, "the CB919 has a seat height")
        self.assertLessEqual(row["values_set"], row["bikes"])

    def test_44o_a_field_can_be_removed_from_one_bike(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Remove From Bike Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]
        adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [self.cb919]})

        _, usage = adm.get(f"/api/admin/fields/{key}/usage")
        self.assertEqual([u["bike_id"] for u in usage["bikes"]], [self.cb919])

        s, b = adm.delete(f"/api/admin/bikes/{self.cb919}/specs/{key}")
        self.assertEqual(s, 200, b)
        self.assertFalse(b["value_destroyed"])

        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        self.assertNotIn("Remove From Bike Test",
                         [sp["label"] for c in specs["categories"] for sp in c["specs"]])
        # removing it again is a 404, not a silent success
        self.assertEqual(adm.delete(f"/api/admin/bikes/{self.cb919}/specs/{key}")[0], 404)

    def test_44p_removing_a_spec_with_a_value_needs_confirming(self):
        """"This field does not belong here" is a different statement from
        "throw away what somebody recorded"."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Valued Removal Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]
        adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [self.cb919]})

        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        spec = next(sp for c in specs["categories"] for sp in c["specs"]
                    if sp["label"] == "Valued Removal Test")
        adm.patch(f"/api/specs/{spec['id']}", {"value": "something real"})

        s, b = adm.delete(f"/api/admin/bikes/{self.cb919}/specs/{key}")
        self.assertEqual(s, 409)
        self.assertIn("destroys them", b["error"])

        # the value survived the refusal
        _, still = adm.get(f"/api/bikes/{self.cb919}/specs")
        self.assertTrue(next(sp for c in still["categories"] for sp in c["specs"]
                             if sp["id"] == spec["id"])["value"])

        s, b = adm.delete(f"/api/admin/bikes/{self.cb919}/specs/{key}", {"force": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["value_destroyed"])

    def test_44q_branches_can_be_removed_without_touching_bikes(self):
        """Detaching a branch stops FUTURE bikes receiving the field; bikes that
        already have it keep it, values and all."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Detach Branch Test",
                                           "category": "Drive", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "trigger_question": "q3", "trigger_option": "A",
            "backfill": True})
        key = r["created_field_key"]
        before = r["backfilled"]
        self.assertGreater(before, 0)

        _, cleared = adm.delete(f"/api/admin/fields/{key}/trigger")
        self.assertEqual(cleared["removed"], 1)

        con = sqlite3.connect(self.db)
        still = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
        con.close()
        self.assertEqual(still, before, "detaching a branch destroyed spec rows")

        # a new chain bike no longer receives it
        bike = self._new_bike(adm, "DETACHED TEST", 1955, 1955)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        self.assertNotIn("Detach Branch Test",
                         [sp["label"] for c in specs["categories"] for sp in c["specs"]])

    def test_44r_force_deleting_a_field_reports_the_damage(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Force Delete Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]
        adm.post(f"/api/admin/fields/{key}/apply", {"bike_ids": [self.cb919]})
        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        spec = next(sp for c in specs["categories"] for sp in c["specs"]
                    if sp["label"] == "Force Delete Test")
        adm.patch(f"/api/specs/{spec['id']}", {"value": "will be lost"})

        # refused without force, and the counts are in the message
        s, b = adm.delete(f"/api/admin/fields/{key}")
        self.assertEqual(s, 409)
        self.assertIn("1 with a value", b["error"])

        s, b = adm.delete(f"/api/admin/fields/{key}", {"force": True})
        self.assertEqual(s, 200)
        self.assertEqual(b["bikes_affected"], 1)
        self.assertEqual(b["values_destroyed"], 1)

        con = sqlite3.connect(self.db)
        left = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?", (key,)).fetchone()[0]
        field = con.execute("SELECT COUNT(*) FROM spec_fields WHERE field_key=?", (key,)).fetchone()[0]
        con.close()
        self.assertEqual((left, field), (0, 0))

    def test_44m_deleting_a_field_in_use_is_refused(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Delete Guard Test",
                                           "category": "General", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {"status": "approved"})
        key = r["created_field_key"]

        # unused: deletable
        self.assertEqual(adm.delete(f"/api/admin/fields/{key}")[0], 200)

        # in use: refused, so real spec rows are never cascaded away
        _, p2 = mgr.post("/api/proposals", {"field_name": "In Use Test",
                                            "category": "General", "reasoning": "x"})
        _, r2 = adm.post(f"/api/admin/proposals/{p2['id']}/decide", {"status": "approved"})
        adm.post(f"/api/admin/fields/{r2['created_field_key']}/apply",
                 {"bike_ids": [self.cb919]})
        s, b = adm.delete(f"/api/admin/fields/{r2['created_field_key']}")
        self.assertEqual(s, 409)
        self.assertIn("already use this field", b["error"])

    def test_45_duplicate_proposal_field_refused(self):
        mgr = self.as_("m.alvarez")
        _, made = mgr.post("/api/proposals", {
            "field_name": "Seat Height", "category": "General",
            "reasoning": "duplicate on purpose"})
        s, b = self.as_("admin").post(f"/api/admin/proposals/{made['id']}/decide",
                                      {"status": "approved"})
        self.assertEqual(s, 409)

    # -- enrichment -------------------------------------------------------
    def test_50_enrichment_reads_tools_and_links(self):
        s, b = self.anon().get(
            f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")
        self.assertEqual(s, 200)
        self.assertEqual(len(b["tools"]), 5)
        self.assertEqual(len(b["links"]), 3)
        self.assertFalse(b["is_manager"])
        votes = [l["votes"] for l in b["links"]]
        self.assertEqual(votes, sorted(votes, reverse=True), "links sort by votes")

    def test_51_bad_link_url_refused(self):
        s, b = self.as_("t.moreno").post(
            f"/api/bikes/{self.cb919}/fields/engine_oil_volume/links",
            {"link_type": "yt", "title": "x", "url": "javascript:alert(1)"})
        self.assertEqual(s, 400)

    def test_52_duplicate_link_refused(self):
        c = self.as_("t.moreno")
        body = {"link_type": "forum", "title": "dupe test",
                "url": "https://example.com/dupe-test"}
        self.assertEqual(c.post(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/links", body)[0], 200)
        self.assertEqual(c.post(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/links", body)[0], 409)

    def test_53_pause_hides_from_readers_but_not_manager(self):
        mgr = self.as_("m.alvarez")
        _, b = mgr.get(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")
        self.assertTrue(b["is_manager"])
        link = b["links"][0]

        self.assertTrue(mgr.post(f"/api/links/{link['id']}/pause")[1]["paused"])

        _, pub = self.anon().get(
            f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")
        self.assertNotIn(link["id"], [l["id"] for l in pub["links"]])

        _, mgr_view = mgr.get(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")
        self.assertIn(link["id"], [l["id"] for l in mgr_view["links"]])

        mgr.post(f"/api/links/{link['id']}/pause")  # restore

    def test_54_user_cannot_pause(self):
        _, b = self.anon().get(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")
        s, _ = self.as_("t.moreno").post(f"/api/links/{b['links'][0]['id']}/pause")
        self.assertEqual(s, 403)

    def test_55_link_flag_once_then_withdraw(self):
        c = self.as_("rider_kestrel99")
        _, b = c.get(f"/api/bikes/{self.cb919}/fields/engine_oil_volume/enrichment")
        lid = b["links"][0]["id"]
        self.assertEqual(c.post(f"/api/links/{lid}/flag", {"reason": "broken"})[0], 200)
        self.assertEqual(c.post(f"/api/links/{lid}/flag", {"reason": "broken"})[0], 409)
        self.assertEqual(c.delete(f"/api/links/{lid}/flag")[0], 200)
        self.assertEqual(c.post(f"/api/links/{lid}/flag", {"reason": "broken"})[0], 200)

    # -- garage and service ----------------------------------------------
    def test_60_garage_is_private(self):
        _, dave = self.as_("cb919_dave").get("/api/garage")
        self.assertEqual(len(dave["garage"]), 1)
        _, other = self.as_("t.moreno").get("/api/garage")
        self.assertEqual(len(other["garage"]), 0)

    def test_61_cannot_touch_another_users_garage(self):
        _, dave = self.as_("cb919_dave").get("/api/garage")
        ub = dave["garage"][0]["id"]
        self.assertEqual(self.as_("t.moreno").patch(f"/api/garage/{ub}", {"mileage": 1})[0], 403)
        self.assertEqual(self.as_("t.moreno").delete(f"/api/garage/{ub}")[0], 403)
        self.assertEqual(self.as_("t.moreno").get(f"/api/garage/{ub}/service")[0], 403)

    def test_62_service_interval_comes_from_the_spec_sheet(self):
        """Correcting the oil-change interval spec must move the rider's
        next-due mileage — that is why the task reads the spec instead of
        holding its own copy."""
        dave = self.as_("cb919_dave")
        _, g = dave.get("/api/garage")
        ub = g["garage"][0]["id"]

        _, before = dave.get(f"/api/garage/{ub}/service")
        oil = next(t for t in before["tasks"] if t["task_key"] == "oil")
        self.assertEqual(oil["interval_miles"], 8000)

        con = sqlite3.connect(self.db)
        con.execute("UPDATE specs SET value='4,000 mi / 6,000 km'"
                    " WHERE bike_id=? AND field_key='oil_change_interval'", (self.cb919,))
        con.commit(); con.close()

        _, after = dave.get(f"/api/garage/{ub}/service")
        oil2 = next(t for t in after["tasks"] if t["task_key"] == "oil")
        self.assertEqual(oil2["interval_miles"], 4000)
        self.assertNotEqual(oil["due_in_miles"], oil2["due_in_miles"])

    def test_63_logging_service_updates_odometer(self):
        dave = self.as_("cb919_dave")
        _, g = dave.get("/api/garage")
        ub, before = g["garage"][0]["id"], g["garage"][0]["mileage"]
        s, _ = dave.post(f"/api/garage/{ub}/service",
                         {"task_key": "oil", "performed_on": "2026-08-20",
                          "miles": before + 500})
        self.assertEqual(s, 200)
        _, g2 = dave.get("/api/garage")
        self.assertEqual(g2["garage"][0]["mileage"], before + 500)

    def test_64_bad_service_date_refused(self):
        dave = self.as_("cb919_dave")
        _, g = dave.get("/api/garage")
        s, _ = dave.post(f"/api/garage/{g['garage'][0]['id']}/service",
                         {"task_key": "oil", "performed_on": "20-08-2026"})
        self.assertEqual(s, 400)

    # -- questionnaire ----------------------------------------------------
    def test_70_questionnaire_lists_only_gaps(self):
        s, b = self.as_("m.alvarez").get(f"/api/questionnaire/{self.cb919}")
        self.assertEqual(s, 200)
        for q in b["questions"]:
            self.assertIn(q["value"], (None, ""))

    def test_71_not_sure_creates_admin_item_and_leaves_gap(self):
        mgr = self.as_("m.alvarez")
        _, q = mgr.get(f"/api/questionnaire/{self.cb919}")
        target = q["questions"][0]
        s, _ = mgr.post(f"/api/questionnaire/{self.cb919}/answer",
                        {"field_key": target["field_key"], "not_sure": True})
        self.assertEqual(s, 200)

        _, again = mgr.get(f"/api/questionnaire/{self.cb919}")
        self.assertIn(target["field_key"], [x["field_key"] for x in again["questions"]],
                      "'not sure' must leave the gap open, not close it")

        _, admin_q = self.as_("admin").get("/api/admin/not-sure")
        self.assertIn(target["label"], [i["question_text"] for i in admin_q["items"]])

    def test_72_admin_confirming_writes_the_value(self):
        adm = self.as_("admin")
        _, items = adm.get("/api/admin/not-sure")
        item = next(i for i in items["items"] if i["field_key"])
        s, _ = adm.post(f"/api/admin/not-sure/{item['id']}/decide",
                        {"status": "confirmed", "value": "Digital CDI"})
        self.assertEqual(s, 200)
        con = sqlite3.connect(self.db)
        v, c = con.execute("SELECT value, confidence FROM specs WHERE bike_id=? AND field_key=?",
                           (item["bike_id"], item["field_key"])).fetchone()
        con.close()
        self.assertEqual(v, "Digital CDI")
        self.assertEqual(c, "confirmed")

    def test_73_confirming_without_a_value_refused(self):
        adm = self.as_("admin")
        _, items = adm.get("/api/admin/not-sure")
        if not items["items"]:
            self.skipTest("nothing pending")
        s, _ = adm.post(f"/api/admin/not-sure/{items['items'][0]['id']}/decide",
                        {"status": "confirmed"})
        self.assertEqual(s, 400)

    # -- questionnaire: building the tree ---------------------------------
    CB750_ANSWERS = {
        "q0": "C", "q1": "A", "q2": "A", "q2b": "A",
        "q2c": "A", "q2ca": "A", "q2cc": "B",     # one headlight, no parking light
        "q3": "A", "q4": "D",
        "q4a": "B", "q5": "C", "q6": "C", "q7": "E", "q8": "B", "q9": "A",
        "q9a": "A", "q10": "B", "q11": "B", "q12": "B", "q13": "B", "q14": "A",
        "q15": "B", "q16": "A", "q17": "A", "q18": "A", "q19": "B", "q20": "C",
        "q20a": "A", "q21": "B",
    }

    def _new_bike(self, adm, model_code, year_start=1978, year_end=1979):
        s, b = adm.post("/api/bikes", {
            "make": "Honda", "model_code": model_code,
            "year_start": year_start, "year_end": year_end})
        self.assertEqual(s, 200, b)
        return b["bike_id"]

    def test_73b_manager_cannot_create_a_bike(self):
        """A manager does not pick up bikes; an admin assigns them. Letting a
        manager create one would be self-assignment through the back door."""
        mgr = self.as_("m.alvarez")
        s, _ = mgr.post("/api/bikes", {
            "make": "Honda", "model_code": "SELFASSIGN TEST", "year_start": 1999})
        self.assertEqual(s, 403)

    def test_73b2_manager_can_answer_the_questionnaire_for_their_own_bike(self):
        """Which of the existing questions apply to this machine is knowledge
        about one bike — exactly what the manager was assigned for. What stays
        with admin is which fields EXIST at all, since a new field lands on the
        whole catalogue."""
        mgr = self.as_("m.alvarez")
        self.assertEqual(mgr.get("/api/questionnaire/definition")[0], 200)
        s, b = mgr.post(f"/api/questionnaire/{self.cb919}/build",
                        {"answers": self.CB750_ANSWERS})
        self.assertEqual(s, 200, b)
        self.assertGreater(b["questions_asked"], 20)

    def test_73b3_manager_cannot_answer_for_a_bike_they_do_not_manage(self):
        adm = self.as_("admin")
        other = self._new_bike(adm, "NOT MINE TEST", 1962, 1962)
        s, b = self.as_("m.alvarez").post(f"/api/questionnaire/{other}/build",
                                          {"answers": self.CB750_ANSWERS})
        self.assertEqual(s, 403)
        self.assertIn("do not manage", b["error"])

    # The wizard keeps an answer when you go back, so the option you chose can
    # be highlighted. That means the payload can carry replies to questions the
    # walk no longer reaches, and those must not count as answers about the bike.
    OFFPATH_ANSWERS = {
        "q0": "C", "q1": "A", "q2": "A", "q2b": "A",
        "q2c": "A", "q2ca": "A", "q2cc": "B",
        "q3": "A", "q4": "D",
        "q4a": "B", "q5": "C", "q6": "C", "q7": "E", "q8": "B",
        "q9": "B",            # no fuel pump, so q9a is never asked...
        "q9a": "A",           # ...but a stale "mechanical" reply is still here
        "q10": "B", "q11": "B", "q12": "B", "q13": "B", "q14": "A", "q15": "B",
        "q16": "A", "q17": "A", "q18": "A", "q19": "B", "q20": "C", "q20a": "A",
        "q21": "B",
    }

    def test_73b5_an_answer_the_walk_skipped_is_not_recorded(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "OFFPATH STORE TEST", 1993, 1993)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": self.OFFPATH_ANSWERS})

        con = sqlite3.connect(self.db)
        stored = dict(con.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?",
            (bike,)).fetchall())
        con.close()
        self.assertEqual(stored.get("q9"), "B")
        self.assertNotIn("q9a", stored,
                         "recorded an answer to a question this bike never reached")

    def test_73b6_an_answer_the_walk_skipped_triggers_nothing(self):
        """A bike with no fuel pump must not receive a mechanical-pump spec
        from a q9a reply left behind by going back."""
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        _, p = mgr.post("/api/proposals", {"field_name": "Mech Pump Diaphragm Test",
                                           "category": "Fuel and Air", "reasoning": "x"})
        _, r = adm.post(f"/api/admin/proposals/{p['id']}/decide", {
            "status": "approved", "trigger_question": "q9a", "trigger_option": "A"})

        bike = self._new_bike(adm, "OFFPATH TRIGGER TEST", 1994, 1994)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": self.OFFPATH_ANSWERS})
        # Not asserting from_approved_branches is 0 — other tests attach fields
        # to branches this bike genuinely matches, and those should arrive. The
        # claim is about this one field, reached only through q9a.
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertNotIn("Mech Pump Diaphragm Test", labels)

        # and the same field DOES arrive when the bike genuinely reaches q9a
        on_path = dict(self.OFFPATH_ANSWERS, q9="A", q9a="A")
        bike2 = self._new_bike(adm, "ONPATH TRIGGER TEST", 1995, 1995)
        adm.post(f"/api/questionnaire/{bike2}/build", {"answers": on_path})
        _, specs2 = adm.get(f"/api/bikes/{bike2}/specs")
        self.assertIn("Mech Pump Diaphragm Test",
                      [sp["label"] for c in specs2["categories"] for sp in c["specs"]])

    def test_73b7_auto_answered_questions_are_still_recorded(self):
        """Answering "no battery" settles "no electric starter" without asking.
        That is still an answer about the bike, so it must be kept — the
        off-path filter must not throw it away."""
        adm = self.as_("admin")
        answers = dict(self.OFFPATH_ANSWERS, q1="B")   # no battery -> q2 auto 'B'
        answers.pop("q2", None)
        answers.pop("q9a", None)
        bike = self._new_bike(adm, "AUTO ANSWER TEST", 1996, 1996)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": answers})

        con = sqlite3.connect(self.db)
        stored = dict(con.execute(
            "SELECT question_id, option_label FROM bike_answers WHERE bike_id=?",
            (bike,)).fetchall())
        con.close()
        self.assertEqual(stored.get("q2"), "B",
                         "the auto-answered question was dropped")

    def test_73b8_saved_answers_come_back_for_review(self):
        """The wizard prefills from these so a re-run highlights what was
        answered rather than starting blank."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "SAVED ANSWERS TEST", 1997, 1997)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})

        _, state = adm.get(f"/api/questionnaire/{bike}")
        self.assertIn("answers", state)
        self.assertEqual(state["answers"].get("q3"), self.CB750_ANSWERS["q3"])
        self.assertEqual(state["answers"].get("q0"), self.CB750_ANSWERS["q0"])

    def test_73b4_reanswering_reports_specs_that_no_longer_apply(self):
        """Re-answering only ever adds. A spec the new answers do not call for
        is reported, not deleted — it may hold a value somebody sourced, and the
        re-run might have been a mis-click."""
        mgr = self.as_("m.alvarez")
        # first pass: a chain-drive bike, which brings the chain fields in
        mgr.post(f"/api/questionnaire/{self.cb919}/build",
                 {"answers": dict(self.CB750_ANSWERS, q3="A")})
        # second pass: say it is shaft drive instead
        _, b = mgr.post(f"/api/questionnaire/{self.cb919}/build",
                        {"answers": dict(self.CB750_ANSWERS, q3="C")})
        stale = [f["label"] for f in b["no_longer_applies"]]
        self.assertTrue(stale, "nothing reported as no longer applicable")

        # and they are still on the bike, not silently removed
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertIn(stale[0], labels)

    def test_73c_created_bike_has_no_manager(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "NOMGR TEST", 1995, 1995)
        _, b = self.anon().get(f"/api/bikes/{bike}")
        self.assertEqual(b["managers"], [], "a new bike starts unmanaged")

    def test_74_build_tree_from_answers(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "CB750K TEST")
        s, b = adm.post(f"/api/questionnaire/{bike}/build",
                        {"answers": self.CB750_ANSWERS})
        self.assertEqual(s, 200, b)
        self.assertGreater(b["fields_triggered"], 20)
        self.assertEqual(b["specs_created"], b["fields_triggered"])
        self.assertEqual(b["bike_type"], "Street bike / Sport bike")

        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertIn("Points Gap", labels)          # q8 = Points
        self.assertIn("Carb 1 Main Jet", labels)     # q7 = 4 carbs
        self.assertNotIn("Drive Belt", labels)       # q3 = chain, not belt
        # Every field starts empty: a new branch is a new question, not an answer.
        for c in specs["categories"]:
            for sp in c["specs"]:
                self.assertIn(sp["value"], (None, ""))

    def test_75_aliases_prevent_duplicate_fields(self):
        """The wizard says 'Intake Valve Clearance', the CB919 sheet says
        'Valve Clearance — Intake'. They must resolve to one field, or the bike
        shows both and one is permanently blank."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "ALIAS TEST", 1980, 1980)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        labels = [sp["label"] for c in specs["categories"] for sp in c["specs"]]
        self.assertIn("Valve Clearance — Intake", labels)
        self.assertNotIn("Intake Valve Clearance", labels)
        self.assertEqual(len(labels), len(set(labels)), "duplicate fields on one bike")

    def test_76_build_rejects_incomplete_answers(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "INCOMPLETE TEST", 1981, 1981)
        s, b = adm.post(f"/api/questionnaire/{bike}/build",
                        {"answers": {"q0": "C", "q1": "A"}})
        self.assertEqual(s, 400)
        self.assertIn("no answer for q2", b["error"])

    def test_77_build_rejects_invented_option(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "BADOPT TEST", 1982, 1982)
        bad = dict(self.CB750_ANSWERS, q3="ZZZ")
        s, b = adm.post(f"/api/questionnaire/{bike}/build", {"answers": bad})
        self.assertEqual(s, 400)
        self.assertIn("not an option", b["error"])

    def test_78_build_not_sure_reaches_admin(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "NOTSURE TEST", 1983, 1983)
        answers = dict(self.CB750_ANSWERS, q13="E")   # "Not 100% sure"
        s, b = adm.post(f"/api/questionnaire/{bike}/build", {"answers": answers})
        self.assertEqual(b["not_sure"], 1)
        _, items = adm.get("/api/admin/not-sure")
        self.assertIn("Does this bike have oil filter?",
                      [i["question_text"] for i in items["items"]])

    def test_79_cannot_claim_a_year_another_bike_owns(self):
        """The schema trigger, surfaced as a clean 409 rather than a 500."""
        adm = self.as_("admin")
        self._new_bike(adm, "OVERLAP TEST", 1990, 1992)
        s, b = adm.post("/api/bikes", {
            "make": "Honda", "model_code": "OVERLAP TEST",
            "year_start": 1991, "year_end": 1993})
        self.assertEqual(s, 409)
        self.assertIn("already claimed", b["error"])

    def test_79a_sibling_under_the_same_model_code_is_allowed(self):
        """The split workflow: a second bike under the SAME make+model_code,
        covering different years. This is what the Add-a-Bike model dropdown
        exists to make easy — picking the existing code keeps both bikes in one
        lineage, so sibling_divergence can still compare them."""
        adm = self.as_("admin")
        first = self._new_bike(adm, "SPLIT TEST", 2002, 2005)
        s, b = adm.post("/api/bikes", {
            "make": "Honda", "model_code": "SPLIT TEST",
            "year_start": 2006, "year_end": 2007})
        self.assertEqual(s, 200, b)
        second = b["bike_id"]
        self.assertNotEqual(first, second)

        con = sqlite3.connect(self.db)
        codes = con.execute(
            "SELECT DISTINCT model_code FROM bikes WHERE id IN (?,?)",
            (first, second)).fetchall()
        years_first = con.execute(
            "SELECT COUNT(*) FROM bike_years WHERE bike_id=?", (first,)).fetchone()[0]
        years_second = con.execute(
            "SELECT COUNT(*) FROM bike_years WHERE bike_id=?", (second,)).fetchone()[0]
        con.close()
        self.assertEqual(len(codes), 1, "siblings must share one model code")
        self.assertEqual((years_first, years_second), (4, 2))

    # -- assigning managers -----------------------------------------------
    def test_79b_only_admin_can_assign_a_manager(self):
        mgr = self.as_("m.alvarez")
        self.assertEqual(mgr.get("/api/admin/assignments")[0], 403)
        s, _ = mgr.post(f"/api/admin/bikes/{self.cb919}/manager",
                        {"user_id": 999})
        self.assertEqual(s, 403)

    def test_79c_assignment_dropdown_has_what_it_needs(self):
        _, b = self.as_("admin").get("/api/admin/assignments")
        self.assertTrue(b["assignments"], "CB919 is seeded with a manager")
        self.assertTrue(b["candidates"], "nobody to assign to")
        self.assertTrue(b["unassigned"], "no unassigned bikes to pick from")
        # Admins reach every bike already, so offering them is a no-op.
        self.assertNotIn("admin", [c["username"] for c in b["candidates"]])
        # A bike that already has a manager is not in the unassigned list.
        self.assertNotIn(self.cb919, [u["bike_id"] for u in b["unassigned"]])

    def test_79d_assigning_promotes_and_grants_access(self):
        """Being handed a bike is what makes someone a bike manager — otherwise
        the assignment exists but every manager route still 403s them."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "ASSIGN TEST", 1993, 1993)

        before = self.as_("t.moreno")
        self.assertEqual(before.get("/api/manager/bikes")[0], 403)

        s, r = adm.post(f"/api/admin/bikes/{bike}/manager", {
            "user_id": self._uid("t.moreno"), "specialty": "Dirt / Off-road"})
        self.assertEqual(s, 200)
        self.assertTrue(r["promoted"])

        after = self.as_("t.moreno")          # fresh session, new role
        s, b = after.get("/api/manager/bikes")
        self.assertEqual(s, 200)
        self.assertIn(bike, [x["bike_id"] for x in b["bikes"]])
        self.assertEqual(b["bikes"][0]["specialty"], "Dirt / Off-road")

    def test_79e_unassigning_last_bike_demotes(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "DEMOTE TEST", 1994, 1994)
        uid = self._uid("rider_kestrel99")
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": uid})
        self.assertEqual(self.as_("rider_kestrel99").get("/api/manager/bikes")[0], 200)

        s, r = adm.delete(f"/api/admin/bikes/{bike}/manager/{uid}")
        self.assertEqual(s, 200)
        self.assertTrue(r["demoted"], "no bikes left, so manager is the wrong label")
        self.assertEqual(self.as_("rider_kestrel99").get("/api/manager/bikes")[0], 403)

    def test_79f_duplicate_and_admin_assignment_refused(self):
        adm = self.as_("admin")
        s, _ = adm.post(f"/api/admin/bikes/{self.cb919}/manager",
                        {"user_id": self._uid("m.alvarez")})
        self.assertEqual(s, 409, "m.alvarez already manages the CB919")
        s, b = adm.post(f"/api/admin/bikes/{self.cb919}/manager",
                        {"user_id": self._uid("admin")})
        self.assertEqual(s, 400)
        self.assertIn("already reach every bike", b["error"])

    def _uid(self, username):
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        con.close()
        return row[0]

    # -- admin ------------------------------------------------------------
    def test_80_catalog_provenance_is_queryable(self):
        s, b = self.as_("admin").get("/api/admin/catalog-dropped?limit=5")
        self.assertEqual(s, 200)
        self.assertEqual(len(b["items"]), 5)
        self.assertGreater(b["total"], 800)

    def test_81_suspension_kills_the_session(self):
        con = sqlite3.connect(self.db)
        h, salt = seed.hash_password("gearhead")
        con.execute("INSERT INTO users (username, role, password_hash, password_salt)"
                    " VALUES ('doomed','user',?,?)", (h, salt))
        con.commit()
        uid = con.execute("SELECT id FROM users WHERE username='doomed'").fetchone()[0]
        con.close()

        victim = self.as_("doomed")
        self.assertEqual(victim.get("/api/garage")[0], 200)

        flagger = self.as_("t.moreno")
        _, f = flagger.post(f"/api/users/{uid}/flags",
                            {"reason": "spam", "detail": "test"})
        self.assertEqual(
            self.as_("admin").post(f"/api/admin/user-flags/{f['id']}/decide",
                                   {"status": "actioned", "suspend": True})[0], 200)

        self.assertEqual(victim.get("/api/garage")[0], 401,
                         "suspending must invalidate existing sessions")
        self.assertEqual(self.anon().login("doomed")[0], 403)

    # -- plumbing ---------------------------------------------------------
    def test_90_unknown_endpoint_404(self):
        self.assertEqual(self.anon().get("/api/nope")[0], 404)

    def test_91_wrong_method_405(self):
        # /api/auth/me is GET-only; a known path with an unknown method is 405,
        # not 404, so a client can tell "wrong verb" from "wrong URL".
        self.assertEqual(self.anon().delete("/api/auth/me")[0], 405)

    def test_92_malformed_json_400(self):
        req = urllib.request.Request(self.base + "/api/auth/login",
                                     data=b"{not json", method="POST")
        req.add_header("Content-Type", "application/json")
        try:
            urllib.request.urlopen(req)
            self.fail("should have failed")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_93_static_path_traversal_blocked(self):
        """A path that climbs out of static/ must not reach the database."""
        for probe in ["/../data.db", "/..%2fdata.db", "/static/../../app.py"]:
            try:
                with urllib.request.urlopen(self.base + probe) as r:
                    body = r.read()
                self.assertNotIn(b"SQLite format", body[:32])
                self.assertNotIn(b"import sqlite3", body)
            except urllib.error.HTTPError as e:
                self.assertIn(e.code, (403, 404))


    # -- lifting a spec into the bike's hero -------------------------------
    #
    # The hero has always shown the General category, which is a property of
    # the field and therefore identical on all 261 bikes. Pinning is the
    # per-bike addition: same shape as the Fixed tag, and scoped the same way,
    # because a manager reshaping one hero must not reshape the other 260.

    def _pinnable(self, client, bike_id):
        """A spec on this bike that is not already in the hero by category."""
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        for cat in specs["categories"]:
            if cat["name"] == "General":
                continue
            for sp in cat["specs"]:
                if not sp["in_header"]:
                    return sp
        self.fail("no pinnable spec on this bike")

    def test_80_manager_pins_a_spec_to_the_header(self):
        mgr = self.as_("m.alvarez")
        spec = self._pinnable(mgr, self.cb919)

        s, b = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": spec["field_key"]})
        self.assertEqual(s, 200)
        self.assertTrue(b["in_header"])

        # and a reader sees it marked, without signing in
        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        found = [x for c in specs["categories"] for x in c["specs"]
                 if x["field_key"] == spec["field_key"]]
        self.assertTrue(found[0]["in_header"])

        mgr.delete(f"/api/bikes/{self.cb919}/header/{spec['field_key']}")

    def test_80b_pinning_is_per_bike_not_per_field(self):
        """The whole point of a pin rather than a category change: the field
        moves in one bike's hero and nowhere else."""
        adm = self.as_("admin")
        other = self._new_bike(adm, "HEADER SCOPE TEST", 1991, 1992)
        adm.post(f"/api/questionnaire/{other}/build",
                 {"answers": self.CB750_ANSWERS})

        spec = self._pinnable(adm, self.cb919)
        adm.post(f"/api/bikes/{self.cb919}/header",
                 {"field_key": spec["field_key"]})

        _, elsewhere = adm.get(f"/api/bikes/{other}/specs")
        same = [x for c in elsewhere["categories"] for x in c["specs"]
                if x["field_key"] == spec["field_key"]]
        for x in same:
            self.assertFalse(x["in_header"],
                             "pinning one bike changed another bike's hero")

        adm.delete(f"/api/bikes/{self.cb919}/header/{spec['field_key']}")

    def test_80c_general_specs_cannot_be_pinned(self):
        """They are in every hero already. Accepting the pin would imply a
        manager had made a choice that the Spec Tree had actually made."""
        mgr = self.as_("m.alvarez")
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        general = [c for c in specs["categories"] if c["name"] == "General"]
        if not general:
            self.skipTest("this bike carries no General specs")
        key = general[0]["specs"][0]["field_key"]

        s, b = mgr.post(f"/api/bikes/{self.cb919}/header", {"field_key": key})
        self.assertEqual(s, 409)
        self.assertIn("already", b["error"])

    def test_80d_unpinning_takes_it_back_out(self):
        mgr = self.as_("m.alvarez")
        spec = self._pinnable(mgr, self.cb919)
        mgr.post(f"/api/bikes/{self.cb919}/header", {"field_key": spec["field_key"]})

        s, _ = mgr.delete(f"/api/bikes/{self.cb919}/header/{spec['field_key']}")
        self.assertEqual(s, 200)

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        again = [x for c in specs["categories"] for x in c["specs"]
                 if x["field_key"] == spec["field_key"]]
        self.assertFalse(again[0]["in_header"])

    def test_80e_pinning_twice_is_harmless(self):
        mgr = self.as_("m.alvarez")
        spec = self._pinnable(mgr, self.cb919)
        for _ in range(2):
            s, _ = mgr.post(f"/api/bikes/{self.cb919}/header",
                            {"field_key": spec["field_key"]})
            self.assertEqual(s, 200)
        mgr.delete(f"/api/bikes/{self.cb919}/header/{spec['field_key']}")

    def test_80f_cannot_pin_a_spec_this_bike_does_not_have(self):
        """Otherwise a hero could advertise a field the machine never had."""
        mgr = self.as_("m.alvarez")
        s, _ = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": "no_such_field_at_all"})
        self.assertEqual(s, 404)

    def test_80g_only_this_bike_s_manager_may_pin(self):
        """Manager rank alone is not enough — the same rule as everywhere
        else a manager acts on a bike."""
        adm = self.as_("admin")
        spec = self._pinnable(adm, self.cb919)

        con = sqlite3.connect(self.db)
        row = con.execute("SELECT id FROM users WHERE username='hdr_mgr'").fetchone()
        if row is None:
            con.execute("INSERT INTO users (username, role, password_hash,"
                        " password_salt) VALUES ('hdr_mgr','manager','x','y')")
            con.commit()
            uid = con.execute("SELECT id FROM users WHERE username='hdr_mgr'"
                              ).fetchone()[0]
            h, salt = seed.hash_password("gearhead")
            con.execute("UPDATE users SET password_hash=?, password_salt=?"
                        " WHERE id=?", (h, salt, uid))
            con.commit()
        con.close()

        s, b = self.as_("hdr_mgr").post(f"/api/bikes/{self.cb919}/header",
                                        {"field_key": spec["field_key"]})
        self.assertEqual(s, 403)
        self.assertIn("do not manage", b["error"])

    def test_80h_readers_and_anonymous_cannot_pin(self):
        adm = self.as_("admin")
        spec = self._pinnable(adm, self.cb919)
        key = spec["field_key"]

        self.assertEqual(self.as_("t.moreno").post(
            f"/api/bikes/{self.cb919}/header", {"field_key": key})[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/bikes/{self.cb919}/header", {"field_key": key})[0], 401)
        self.assertEqual(self.anon().delete(
            f"/api/bikes/{self.cb919}/header/{key}")[0], 401)

    def test_80i_unpinning_something_unpinned_is_a_404(self):
        mgr = self.as_("m.alvarez")
        spec = self._pinnable(mgr, self.cb919)
        s, _ = mgr.delete(f"/api/bikes/{self.cb919}/header/{spec['field_key']}")
        self.assertEqual(s, 404)

    # -- re-filing a field into another category ---------------------------
    #
    # Category sits on the field, so a move lands on every bike at once. That
    # is why it is admin-only, and why sort_order has to be re-derived: the old
    # number belongs to the old category's 1000-wide band, and the browse page
    # groups by consecutive runs.

    def _a_field(self, client, category=None):
        _, f = client.get("/api/admin/fields?limit=200")
        for x in f["fields"]:
            if category is None or x["category"] == category:
                return x
        self.fail(f"no field in {category}")

    def test_82_admin_moves_a_field_to_another_category(self):
        adm = self.as_("admin")
        field = self._a_field(adm, "Engine")
        was = field["category"]

        s, b = adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                         {"category": "Controls"})
        self.assertEqual(s, 200)
        self.assertEqual(b["category"], "Controls")

        _, f = adm.get("/api/admin/fields?limit=200")
        now = [x for x in f["fields"] if x["field_key"] == field["field_key"]][0]
        self.assertEqual(now["category"], "Controls")

        adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                  {"category": was})

    def test_82b_the_move_lands_it_in_the_new_band(self):
        """Contiguity is the whole point: the browse page starts a new heading
        at every change of category as it walks sort_order, so a field carrying
        its old number into a new category would split that category in two."""
        adm = self.as_("admin")
        field = self._a_field(adm, "Electrical")
        was = field["category"]

        _, b = adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                         {"category": "Suspension"})

        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT category, MIN(sort_order), MAX(sort_order) FROM spec_fields"
            " GROUP BY category").fetchall()
        bands = {c: (lo, hi) for c, lo, hi in rows}
        con.close()

        lo, hi = bands["Suspension"]
        self.assertGreaterEqual(b["sort_order"], lo)
        self.assertLessEqual(b["sort_order"], hi)
        # and no other category's band overlaps it
        for cat, (clo, chi) in bands.items():
            if cat == "Suspension":
                continue
            self.assertFalse(clo <= b["sort_order"] <= chi,
                             f"landed inside {cat}'s band too")

        adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                  {"category": was})

    def test_82c_the_move_touches_no_spec_values(self):
        """Spec rows reference the field, not the category. A re-file must not
        put a single sourced value at risk."""
        adm = self.as_("admin")
        field = self._a_field(adm, "Fuel and Air")
        con = sqlite3.connect(self.db)
        before = con.execute(
            "SELECT COUNT(*) FROM specs WHERE field_key=? AND value IS NOT NULL",
            (field["field_key"],)).fetchone()[0]
        con.close()

        adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                  {"category": "General"})

        con = sqlite3.connect(self.db)
        after = con.execute(
            "SELECT COUNT(*) FROM specs WHERE field_key=? AND value IS NOT NULL",
            (field["field_key"],)).fetchone()[0]
        con.close()
        self.assertEqual(before, after)

        adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                  {"category": "Fuel and Air"})

    def test_82d_rubbish_categories_are_refused(self):
        adm = self.as_("admin")
        field = self._a_field(adm)
        s, b = adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                         {"category": "Wheels and Tyres"})
        self.assertEqual(s, 400)
        self.assertIn("must be one of", b["error"])

    def test_82e_moving_a_field_where_it_already_is_is_refused(self):
        adm = self.as_("admin")
        field = self._a_field(adm)
        s, b = adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                         {"category": field["category"]})
        self.assertEqual(s, 409)
        self.assertIn("already in", b["error"])

    def test_82f_only_admin_may_re_file(self):
        """It changes the spec sheet of all 261 bikes. A manager is scoped to
        their own, which is the opposite of that."""
        adm = self.as_("admin")
        field = self._a_field(adm)
        self.assertEqual(self.as_("m.alvarez").patch(
            f"/api/admin/fields/{field['field_key']}/category",
            {"category": "General"})[0], 403)
        self.assertEqual(self.anon().patch(
            f"/api/admin/fields/{field['field_key']}/category",
            {"category": "General"})[0], 401)

    def test_82g_the_move_is_recorded(self):
        adm = self.as_("admin")
        field = self._a_field(adm, "Engine")
        was = field["category"]
        adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                  {"category": "Drive"})

        _, act = adm.get("/api/admin/activity")
        mine = [a for a in act["actions"] if a["action"] == "field.category"]
        self.assertTrue(mine, "the re-file left no audit row")
        self.assertIn("Drive", mine[0]["summary"])

        adm.patch(f"/api/admin/fields/{field['field_key']}/category",
                  {"category": was})

    def test_82h_admin_can_re_file_a_proposal_at_approval(self):
        """Same reasoning as rewording the name: the proposer chose from a
        dropdown without seeing how the rest of the tree is arranged."""
        mgr = self.as_("m.alvarez")
        s, prop = mgr.post("/api/proposals", {
            "field_name": "Fork Brace Torque",
            "category": "Engine",              # wrong on purpose
            "reasoning": "belongs under Suspension, filed under Engine",
        })
        self.assertEqual(s, 200)

        adm = self.as_("admin")
        s, r = adm.post(f"/api/admin/proposals/{prop['id']}/decide", {
            "status": "approved",
            "field_name": "Fork Brace Torque",
            "category": "Suspension",
            "triggers": [],
        })
        self.assertEqual(s, 200)

        _, f = adm.get("/api/admin/fields?limit=200")
        made = [x for x in f["fields"]
                if x["field_key"] == r["created_field_key"]][0]
        self.assertEqual(made["category"], "Suspension")

    # -- renaming a field --------------------------------------------------
    #
    # label is what riders read; field_key is what specs, field_triggers,
    # bike_header_specs and the Guides links all point at. A rename moves the
    # first and must never touch the second — that separation is the reason a
    # rename is one UPDATE instead of a migration.

    def test_84_admin_renames_a_field(self):
        adm = self.as_("admin")
        field = self._a_field(adm, "Engine")
        was = field["label"]

        s, b = adm.patch(f"/api/admin/fields/{field['field_key']}/label",
                         {"label": "Renamed By Test"})
        self.assertEqual(s, 200)
        self.assertEqual(b["label"], "Renamed By Test")
        self.assertEqual(b["was"], was)

        _, f = adm.get("/api/admin/fields?limit=200")
        now = [x for x in f["fields"] if x["field_key"] == field["field_key"]][0]
        self.assertEqual(now["label"], "Renamed By Test")

        adm.patch(f"/api/admin/fields/{field['field_key']}/label", {"label": was})

    def test_84b_the_key_and_the_spec_rows_do_not_move(self):
        """The whole point: renaming is cosmetic, so nothing that references
        the field by key can break."""
        adm = self.as_("admin")
        field = self._a_field(adm, "Drive")
        was = field["label"]
        key = field["field_key"]

        con = sqlite3.connect(self.db)
        before = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                             (key,)).fetchone()[0]
        con.close()

        adm.patch(f"/api/admin/fields/{key}/label", {"label": "Still The Same Row"})

        con = sqlite3.connect(self.db)
        after = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                            (key,)).fetchone()[0]
        key_intact = con.execute(
            "SELECT COUNT(*) FROM spec_fields WHERE field_key=?", (key,)).fetchone()[0]
        con.close()
        self.assertEqual(before, after, "spec rows lost their field")
        self.assertEqual(key_intact, 1, "field_key changed under a rename")

        adm.patch(f"/api/admin/fields/{key}/label", {"label": was})

    def test_84c_readers_see_the_new_name(self):
        adm = self.as_("admin")
        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        spec = specs["categories"][0]["specs"][0]
        was = spec["label"]

        adm.patch(f"/api/admin/fields/{spec['field_key']}/label",
                  {"label": "Visible To Riders"})

        _, after = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        labels = [x["label"] for c in after["categories"] for x in c["specs"]]
        self.assertIn("Visible To Riders", labels)
        self.assertNotIn(was, labels)

        adm.patch(f"/api/admin/fields/{spec['field_key']}/label", {"label": was})

    def test_84d_two_fields_cannot_share_a_name(self):
        """Whatever the keys say, two rows reading the same on a spec sheet are
        indistinguishable to the person reading it."""
        adm = self.as_("admin")
        _, f = adm.get("/api/admin/fields?limit=200")
        a, b = f["fields"][0], f["fields"][1]

        s, body = adm.patch(f"/api/admin/fields/{a['field_key']}/label",
                            {"label": b["label"]})
        self.assertEqual(s, 409)
        self.assertIn("already the name", body["error"])

        # and the check ignores case and surrounding space
        s, _ = adm.patch(f"/api/admin/fields/{a['field_key']}/label",
                         {"label": "  " + b["label"].upper() + "  "})
        self.assertEqual(s, 409)

    def test_84e_renaming_to_the_same_name_is_refused(self):
        adm = self.as_("admin")
        field = self._a_field(adm)
        s, b = adm.patch(f"/api/admin/fields/{field['field_key']}/label",
                         {"label": field["label"]})
        self.assertEqual(s, 409)
        self.assertIn("already its name", b["error"])

    def test_84f_a_blank_name_is_refused(self):
        adm = self.as_("admin")
        field = self._a_field(adm)
        self.assertEqual(adm.patch(
            f"/api/admin/fields/{field['field_key']}/label", {"label": "   "})[0], 400)

    def test_84g_only_admin_may_rename(self):
        adm = self.as_("admin")
        field = self._a_field(adm)
        self.assertEqual(self.as_("m.alvarez").patch(
            f"/api/admin/fields/{field['field_key']}/label",
            {"label": "Manager Renamed This"})[0], 403)
        self.assertEqual(self.as_("t.moreno").patch(
            f"/api/admin/fields/{field['field_key']}/label",
            {"label": "Reader Renamed This"})[0], 403)
        self.assertEqual(self.anon().patch(
            f"/api/admin/fields/{field['field_key']}/label",
            {"label": "Nobody Renamed This"})[0], 401)

    def test_84h_the_rename_is_recorded_with_both_names(self):
        adm = self.as_("admin")
        field = self._a_field(adm, "Electrical")
        was = field["label"]
        adm.patch(f"/api/admin/fields/{field['field_key']}/label",
                  {"label": "Audited Rename"})

        _, act = adm.get("/api/admin/activity")
        mine = [a for a in act["actions"] if a["action"] == "field.rename"]
        self.assertTrue(mine, "the rename left no audit row")
        self.assertIn("Audited Rename", mine[0]["summary"])
        self.assertIn(was, mine[0]["summary"])

        adm.patch(f"/api/admin/fields/{field['field_key']}/label", {"label": was})

    # -- taking a spec offline ---------------------------------------------
    #
    # The same hide-without-destroying idea tools and links already had, applied
    # to the value. A spec goes offline when it might be wrong: leaving it up
    # means riders keep reading a number as fact while it is in doubt, and
    # deleting it would take the value, its alternates and their votes too.

    def _spec_with_value(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        for c in specs["categories"]:
            for sp in c["specs"]:
                if sp["value"] and not sp["paused"]:
                    return sp
        self.fail("no spec with a value on this bike")

    def test_86_manager_takes_a_spec_offline(self):
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)

        s, b = mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["paused"])

        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    def test_86b_an_offline_spec_is_not_on_a_rider_s_sheet_at_all(self):
        """Gone, not greyed out. A row that names the field while withholding
        the value still tells a rider the bike has that spec."""
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})

        for who in (self.anon(), self.as_("t.moreno")):
            _, specs = who.get(f"/api/bikes/{self.cb919}/specs")
            seen = [x for c in specs["categories"] for x in c["specs"]
                    if x["id"] == spec["id"]]
            self.assertEqual(seen, [], "an offline spec was still on the sheet")
            labels = [x["label"] for c in specs["categories"] for x in c["specs"]]
            self.assertNotIn(spec["label"], labels)

        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    def test_86b2_the_counts_agree_with_the_rows(self):
        """Counting a spec nobody can see would make "56 of 65 filled"
        disagree with the rows underneath it."""
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)

        _, before = self.anon().get(f"/api/bikes/{self.cb919}")
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})

        _, bike = self.anon().get(f"/api/bikes/{self.cb919}")
        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        visible = sum(len(c["specs"]) for c in specs["categories"])
        self.assertEqual(bike["fields_triggered"], visible)
        self.assertEqual(bike["fields_triggered"], before["fields_triggered"] - 1)
        self.assertEqual(bike["specs_filled"], before["specs_filled"] - 1)

        # the manager still counts it — it is their workload
        _, mineBike = mgr.get(f"/api/bikes/{self.cb919}")
        self.assertEqual(mineBike["fields_triggered"], before["fields_triggered"])

        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    def test_86b3_a_category_emptied_by_hiding_does_not_render(self):
        """An empty heading, and a jump-to entry that scrolls to nothing, would
        both be artefacts of hiding rather than facts about the bike."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "EMPTY CATEGORY TEST", 1996, 1996)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": self.CB750_ANSWERS})

        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        smallest = min(specs["categories"], key=lambda c: len(c["specs"]))
        for sp in smallest["specs"]:
            adm.post(f"/api/specs/{sp['id']}/pause", {"paused": True})

        _, public = self.anon().get(f"/api/bikes/{bike}/specs")
        names = [c["name"] for c in public["categories"]]
        self.assertNotIn(smallest["name"], names)
        for c in public["categories"]:
            self.assertTrue(c["specs"], f"{c['name']} rendered with no specs")

    def test_86c_the_manager_still_sees_everything(self):
        """They took it offline to work on it; hiding it from them too would
        make the pause useless."""
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        mine = [x for c in specs["categories"] for x in c["specs"]
                if x["id"] == spec["id"]]
        self.assertEqual(len(mine), 1, "the manager lost sight of their own spec")
        self.assertEqual(mine[0]["value"], spec["value"])
        self.assertTrue(mine[0]["paused"])

        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    def test_86d_offline_closes_votes_flags_and_submissions(self):
        """Hiding the value without closing these would leave riders voting on
        a number they cannot see."""
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})

        rider = self.as_("t.moreno")
        self.assertEqual(rider.post(f"/api/specs/{spec['id']}/vote")[0], 409)
        self.assertEqual(rider.post(f"/api/specs/{spec['id']}/flags",
                                    {"reason": "incorrect"})[0], 409)
        self.assertEqual(rider.post(f"/api/specs/{spec['id']}/alternates",
                                    {"text": "something else"})[0], 409)
        self.assertEqual(rider.post(f"/api/specs/{spec['id']}/request")[0], 409)
        self.assertEqual(rider.post(f"/api/specs/{spec['id']}/value",
                                    {"value": "sneaky"})[0], 409)

        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    def test_86e_nothing_is_destroyed_by_going_offline(self):
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)
        con = sqlite3.connect(self.db)
        before = con.execute(
            "SELECT value, (SELECT COUNT(*) FROM spec_alternates WHERE spec_id=?)"
            " FROM specs WHERE id=?", (spec["id"], spec["id"])).fetchone()
        con.close()

        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

        con = sqlite3.connect(self.db)
        after = con.execute(
            "SELECT value, (SELECT COUNT(*) FROM spec_alternates WHERE spec_id=?)"
            " FROM specs WHERE id=?", (spec["id"], spec["id"])).fetchone()
        con.close()
        self.assertEqual(tuple(before), tuple(after))

    def test_86f_coming_back_online_restores_the_value(self):
        mgr = self.as_("m.alvarez")
        spec = self._spec_with_value(mgr, self.cb919)
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        back = [x for c in specs["categories"] for x in c["specs"]
                if x["id"] == spec["id"]][0]
        self.assertFalse(back["paused"])
        self.assertEqual(back["value"], spec["value"])

    def test_86g_only_this_bike_s_manager_may_pause(self):
        adm = self.as_("admin")
        spec = self._spec_with_value(adm, self.cb919)
        self.assertEqual(self.as_("t.moreno").post(
            f"/api/specs/{spec['id']}/pause", {"paused": True})[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/specs/{spec['id']}/pause", {"paused": True})[0], 401)

    def test_86h_admin_may_pause_any_bike_s_spec(self):
        adm = self.as_("admin")
        spec = self._spec_with_value(adm, self.cb919)
        s, b = adm.post(f"/api/specs/{spec['id']}/pause", {"paused": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["paused"])
        adm.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    def test_86i_pausing_is_per_spec_not_per_field(self):
        """Offline on one machine says nothing about the same field on the
        other 260."""
        adm = self.as_("admin")
        spec = self._spec_with_value(adm, self.cb919)
        other = self._new_bike(adm, "OFFLINE SCOPE TEST", 1993, 1993)
        adm.post(f"/api/questionnaire/{other}/build",
                 {"answers": self.CB750_ANSWERS})

        adm.post(f"/api/specs/{spec['id']}/pause", {"paused": True})

        _, elsewhere = adm.get(f"/api/bikes/{other}/specs")
        same = [x for c in elsewhere["categories"] for x in c["specs"]
                if x["field_key"] == spec["field_key"]]
        for x in same:
            self.assertFalse(x["paused"], "pausing one bike muted another")

        adm.post(f"/api/specs/{spec['id']}/pause", {"paused": False})

    # -- the spec tree reference -------------------------------------------

    def test_88_spec_tree_is_manager_and_admin_only(self):
        """A rider sees field names on any spec sheet; what is withheld is the
        shape of the tree itself, including the fields that reach nobody.

        The plain reader is made here rather than borrowed from the seed: the
        seeded ones get promoted by the assignment tests, which run first.
        """
        con = sqlite3.connect(self.db)
        if not con.execute("SELECT 1 FROM users WHERE username='tree_reader'").fetchone():
            con.execute("INSERT INTO users (username, role, password_hash,"
                        " password_salt) VALUES ('tree_reader','user','x','y')")
            con.commit()
            h, salt = seed.hash_password("gearhead")
            con.execute("UPDATE users SET password_hash=?, password_salt=?"
                        " WHERE username='tree_reader'", (h, salt))
            con.commit()
        con.close()

        self.assertEqual(self.anon().get("/api/spec-tree")[0], 401)
        self.assertEqual(self.as_("tree_reader").get("/api/spec-tree")[0], 403)
        self.assertEqual(self.as_("m.alvarez").get("/api/spec-tree")[0], 200)
        self.assertEqual(self.as_("admin").get("/api/spec-tree")[0], 200)

    def test_88b_the_tree_lists_every_field_once(self):
        """One row per field. A field on two branches must not appear twice,
        nor have its bike counts multiplied by the join."""
        adm = self.as_("admin")
        _, tree = adm.get("/api/spec-tree")

        con = sqlite3.connect(self.db)
        total = con.execute("SELECT COUNT(*) FROM spec_fields").fetchone()[0]
        con.close()

        self.assertEqual(len(tree["fields"]), total)
        keys = [f["field_key"] for f in tree["fields"]]
        self.assertEqual(len(keys), len(set(keys)), "a field was listed twice")

    def test_88c_counts_are_not_inflated_by_multiple_branches(self):
        adm = self.as_("admin")
        _, tree = adm.get("/api/spec-tree")
        multi = [f for f in tree["fields"] if len(f["triggers"]) > 1]
        if not multi:
            self.skipTest("no field sits on two branches in this database")

        con = sqlite3.connect(self.db)
        for f in multi:
            real = con.execute("SELECT COUNT(*) FROM specs WHERE field_key=?",
                               (f["field_key"],)).fetchone()[0]
            self.assertEqual(f["bikes"], real,
                             f"{f['field_key']} bike count multiplied by its branches")
        con.close()

    def test_88d_branches_carry_the_answer_wording(self):
        """"q3=B" is precise and unreadable. The page shows what the person
        answering actually chose."""
        adm = self.as_("admin")
        _, tree = adm.get("/api/spec-tree")
        branched = [f for f in tree["fields"] if f["triggers"]]
        self.assertTrue(branched, "no field is on a branch")
        for t in branched[0]["triggers"]:
            self.assertIn("question_id", t)
            self.assertIn("option", t)
            self.assertTrue(t["text"], "branch has no readable wording")

    # -- the questionnaire and the field registry must agree ---------------
    #
    # build_spec_tree refuses to invent a field it has not seen, so a label the
    # questionnaire asks for and the registry lacks is not a warning: it is a
    # 500 for every bike answering that way. Deleting "Chain Pitch" did exactly
    # that to "Chain and sprockets", the commonest drive system on the platform.

    def test_90a_every_triggerable_field_is_registered(self):
        """The guard for the whole class of bug, not just the two instances."""
        import re as _re
        con = sqlite3.connect(self.db)
        missing = []
        for label in questionnaire.all_triggerable_fields():
            key = _re.sub(r"[^a-z0-9]+", "_",
                          label.lower().replace("×", " x ")
                          .replace("&", " and ")).strip("_")
            if not con.execute("SELECT 1 FROM spec_fields WHERE field_key=?",
                               (key,)).fetchone():
                missing.append(label)
        con.close()
        self.assertEqual(missing, [],
                         "these paths would return a 500 when answered")

    def test_90b_every_path_through_the_questionnaire_can_be_answered(self):
        """Walk each question's options and confirm the build succeeds. A path
        nobody has taken yet is exactly where this rots unnoticed."""
        adm = self.as_("admin")
        defn = questionnaire.load()["questions"]
        base = dict(self.CB750_ANSWERS)

        failures = []
        for qid, q in defn.items():
            for opt in q["options"]:
                answers = dict(base)
                answers[qid] = opt["l"]
                bike = self._new_bike(adm, f"PATH {qid}{opt['l']}", 1990, 1990)
                st, body = adm.post(f"/api/questionnaire/{bike}/build",
                                    {"answers": answers})
                if st >= 500:
                    failures.append(f"{qid}={opt['l']}: {body.get('error')}")
        self.assertEqual(failures, [], "unanswerable paths")

    def test_90c_deleting_a_field_the_questionnaire_asks_for_is_refused(self):
        """Not a confirm-and-proceed: force does not override it. The fix is to
        edit the questionnaire, not to accept a broken one."""
        adm = self.as_("admin")
        asked = None
        for label in questionnaire.all_triggerable_fields():
            hits = questionnaire.questions_triggering(label)
            if hits:
                asked = (label, hits)
                break
        self.assertIsNotNone(asked, "no field is triggered by any answer")

        import re as _re
        key = _re.sub(r"[^a-z0-9]+", "_",
                      asked[0].lower().replace("×", " x ")
                      .replace("&", " and ")).strip("_")

        s, b = adm.delete(f"/api/admin/fields/{key}")
        self.assertEqual(s, 409)
        self.assertIn("questionnaire still asks", b["error"])

        s, b = adm.delete(f"/api/admin/fields/{key}", {"force": True})
        self.assertEqual(s, 409, "force must not override this one")

    def test_90d_a_field_nothing_asks_for_can_still_be_deleted(self):
        """The guard must not lock every field down."""
        adm = self.as_("admin")
        s, prop = self.as_("m.alvarez").post("/api/proposals", {
            "field_name": "Deletable Orphan Field", "category": "Controls",
            "reasoning": "created to be deleted"})
        self.assertEqual(s, 200)
        s, r = adm.post(f"/api/admin/proposals/{prop['id']}/decide",
                        {"status": "approved", "field_name": "Deletable Orphan Field",
                         "triggers": []})
        self.assertEqual(s, 200)

        s, _ = adm.delete(f"/api/admin/fields/{r['created_field_key']}")
        self.assertEqual(s, 200)

    # -- registration -------------------------------------------------------
    #
    # The security property that matters: whatever the body says, the account
    # comes out as a plain reader. Manager is granted by an admin assigning a
    # bike; admin is not grantable through the API at all.

    def test_92a_anyone_can_register_and_is_signed_in(self):
        c = Client(self.base)
        s, b = c.post("/api/auth/register",
                      {"username": "newrider", "email": "newrider@example.com", "password": "a-good-long-one",
                       "display_name": "New Rider"})
        self.assertEqual(s, 200)
        self.assertEqual(b["user"]["username"], "newrider")
        self.assertEqual(b["user"]["role"], "user")

        # the same client is now signed in, without logging in again
        s, me = c.get("/api/auth/me")
        self.assertEqual(me["user"]["username"], "newrider")

    def test_92b_registration_cannot_grant_itself_a_role(self):
        """The one that would hand over the platform."""
        for role in ("admin", "manager"):
            c = Client(self.base)
            s, b = c.post("/api/auth/register",
                          {"username": f"climber_{role}", "email": f"climber_{role}@example.com", "password": "a-good-long-one",
                           "role": role})
            self.assertEqual(s, 200)
            self.assertEqual(b["user"]["role"], "user",
                             f"registering with role={role!r} escalated")

            con = sqlite3.connect(self.db)
            actual = con.execute("SELECT role FROM users WHERE username=?",
                                 (f"climber_{role}",)).fetchone()[0]
            con.close()
            self.assertEqual(actual, "user", "the database took the claimed role")

    def test_92c_a_taken_username_is_refused(self):
        c = Client(self.base)
        s, b = c.post("/api/auth/register",
                      {"username": "admin", "email": "admin@example.com", "password": "a-good-long-one"})
        self.assertEqual(s, 409)
        self.assertIn("taken", b["error"])

    def test_92d_weak_and_malformed_input_is_refused(self):
        cases = [
            ({"username": "ab", "password": "a-good-long-one"}, "too short"),
            ({"username": "a" * 33, "password": "a-good-long-one"}, "too long"),
            ({"username": ".leading", "password": "a-good-long-one"}, "bad first char"),
            ({"username": "has space", "password": "a-good-long-one"}, "space"),
            ({"username": "has/slash", "password": "a-good-long-one"}, "slash"),
            ({"username": "goodname", "password": "short"}, "short password"),
            ({"username": "goodname", "password": "gearhead"}, "site name"),
            ({"username": "samesame", "password": "samesame"}, "password is username"),
        ]
        for body, why in cases:
            s, _ = Client(self.base).post("/api/auth/register", body)
            self.assertEqual(s, 400, f"accepted {why}: {body}")

    def test_92e_the_password_is_not_stored_in_the_clear(self):
        c = Client(self.base)
        c.post("/api/auth/register",
               {"username": "hashcheck", "email": "hashcheck@example.com", "password": "a-good-long-one"})
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT password_hash, password_salt FROM users"
                          " WHERE username='hashcheck'").fetchone()
        con.close()
        self.assertNotIn("a-good-long-one", row[0])
        self.assertTrue(row[1], "no salt stored")
        self.assertEqual(len(row[0]), 64, "not a sha256 hex digest")

    def test_92f_a_registered_account_can_sign_in_again(self):
        Client(self.base).post("/api/auth/register",
                               {"username": "returning", "email": "returning@example.com", "password": "a-good-long-one"})
        c = Client(self.base)
        s, _ = c.post("/api/auth/login",
                      {"username": "returning", "password": "a-good-long-one"})
        self.assertEqual(s, 200)
        self.assertEqual(c.post("/api/auth/login",
                                {"username": "returning", "password": "wrong"})[0], 401)

    def test_92g_a_new_account_has_a_reader_s_powers_and_no_more(self):
        c = Client(self.base)
        c.post("/api/auth/register",
               {"username": "justareader", "email": "justareader@example.com", "password": "a-good-long-one"})
        self.assertEqual(c.get("/api/manager/bikes")[0], 403)
        self.assertEqual(c.get("/api/admin/summary")[0], 403)
        self.assertEqual(c.get("/api/spec-tree")[0], 403)
        # but reading the catalog works
        self.assertEqual(c.get(f"/api/bikes/{self.cb919}/specs")[0], 200)

    # -- adding a spec directly ---------------------------------------------
    #
    # Until this existed, creating a field meant approving a branch proposal,
    # so an admin who wanted one had to file it as a manager and approve their
    # own request. Placement is part of creation here: a field with no branch,
    # no universal flag and no named bike reaches nobody.

    def test_94a_admin_creates_a_field_on_a_branch(self):
        """Answer a bike onto the branch first, so the back-fill has something
        to reach and the count means something."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "BRANCH TARGET FOR NEW FIELD", 1995, 1995)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": dict(self.CB750_ANSWERS, q3="A")})

        s, b = adm.post("/api/admin/fields", {
            "label": "Damper Preload Setting", "category": "Suspension",
            "triggers": [{"question": "q3", "option": "A"}], "backfill": True})
        self.assertEqual(s, 200)
        self.assertEqual(b["category"], "Suspension")
        self.assertGreaterEqual(b["rows_added"], 1,
                                "back-fill reached no bike that answered q3=A")

        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        labels = [x["label"] for c in specs["categories"] for x in c["specs"]]
        self.assertIn("Damper Preload Setting", labels)

        _, tree = adm.get("/api/spec-tree")
        made = [f for f in tree["fields"] if f["field_key"] == b["field_key"]][0]
        self.assertEqual([t["question_id"] for t in made["triggers"]], ["q3"])

    def test_94b_admin_creates_a_universal_field(self):
        adm = self.as_("admin")
        con = sqlite3.connect(self.db)
        total = con.execute("SELECT COUNT(*) FROM bikes").fetchone()[0]
        con.close()

        s, b = adm.post("/api/admin/fields", {
            "label": "Tyre Pressure Gauge", "category": "General",
            "universal": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["universal"])
        self.assertEqual(b["rows_added"], total, "universal missed some bikes")

    def test_94c_admin_creates_a_field_on_named_bikes(self):
        adm = self.as_("admin")
        s, b = adm.post("/api/admin/fields", {
            "label": "Sidecar Mount Torque", "category": "Controls",
            "bike_ids": [self.cb919]})
        self.assertEqual(s, 200)
        self.assertEqual(b["rows_added"], 1)

        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        labels = [x["label"] for c in specs["categories"] for x in c["specs"]]
        self.assertIn("Sidecar Mount Torque", labels)

    def test_94d_a_field_that_reaches_nobody_is_refused(self):
        """The "Fields on no bike" panel is full of these. Creating one is a
        mistake at the moment it is made, not a state to clean up later."""
        adm = self.as_("admin")
        s, b = adm.post("/api/admin/fields",
                        {"label": "Reaches Nobody", "category": "Engine"})
        self.assertEqual(s, 400)
        self.assertIn("say where it goes", b["error"])

    def test_94e_universal_and_a_branch_together_are_refused(self):
        """Universal silently wins over a branch restriction — that is how a
        2-stroke oil spec ended up on four-strokes."""
        adm = self.as_("admin")
        s, b = adm.post("/api/admin/fields", {
            "label": "Contradictory Field", "category": "Engine",
            "universal": True,
            "triggers": [{"question": "q5", "option": "A"}]})
        self.assertEqual(s, 409)
        self.assertIn("cannot be on every bike", b["error"])

    def test_94f_duplicate_and_malformed_names_are_refused(self):
        adm = self.as_("admin")
        _, f = adm.get("/api/admin/fields?limit=200")
        taken = f["fields"][0]["label"]
        s, b = adm.post("/api/admin/fields", {
            "label": taken, "category": "Engine", "universal": True})
        self.assertEqual(s, 409)
        self.assertIn("already exists", b["error"])

        s, _ = adm.post("/api/admin/fields", {
            "label": "!!!", "category": "Engine", "universal": True})
        self.assertEqual(s, 400)

        s, _ = adm.post("/api/admin/fields", {
            "label": "Bad Category Field", "category": "Wheels",
            "universal": True})
        self.assertEqual(s, 400)

    def test_94g_only_admin_may_add_a_spec(self):
        """It creates the field across the whole platform."""
        body = {"label": "Manager Made This", "category": "Engine",
                "universal": True}
        self.assertEqual(self.as_("m.alvarez").post("/api/admin/fields", body)[0], 403)
        self.assertEqual(self.anon().post("/api/admin/fields", body)[0], 401)

    def test_94h_the_creation_is_recorded(self):
        adm = self.as_("admin")
        adm.post("/api/admin/fields", {
            "label": "Audited New Field", "category": "Controls",
            "universal": True})
        _, act = adm.get("/api/admin/activity")
        mine = [a for a in act["actions"] if a["action"] == "field.create"]
        self.assertTrue(mine, "adding a spec left no audit row")
        self.assertIn("Audited New Field", mine[0]["summary"])

    # -- staging a spec offline ---------------------------------------------
    #
    # The field goes on the tree and attaches to its bikes, but no rider sees it
    # until it is switched on. Reuses the per-spec offline flag rather than a
    # second concept, so a staged spec and a spec pulled for review behave the
    # same way to a reader.

    def test_96a_a_spec_can_be_created_offline(self):
        adm = self.as_("admin")
        s, b = adm.post("/api/admin/fields", {
            "label": "Staged Not Live", "category": "Controls",
            "universal": True, "offline": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["offline"])
        self.assertGreater(b["rows_added"], 0)

        con = sqlite3.connect(self.db)
        live, off = con.execute(
            "SELECT SUM(paused=0), SUM(paused=1) FROM specs WHERE field_key=?",
            (b["field_key"],)).fetchone()
        con.close()
        self.assertEqual(live, 0, "some rows went live despite offline")
        self.assertEqual(off, b["rows_added"])

    def test_96b_riders_never_see_a_staged_spec(self):
        adm = self.as_("admin")
        s, b = adm.post("/api/admin/fields", {
            "label": "Invisible While Staged", "category": "Controls",
            "bike_ids": [self.cb919], "offline": True})
        self.assertEqual(s, 200)

        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        labels = [x["label"] for c in specs["categories"] for x in c["specs"]]
        self.assertNotIn("Invisible While Staged", labels)

        # but the bike's manager does, so they can work on it
        _, mine = self.as_("m.alvarez").get(f"/api/bikes/{self.cb919}/specs")
        mlabels = [x["label"] for c in mine["categories"] for x in c["specs"]]
        self.assertIn("Invisible While Staged", mlabels)

    def test_96c_one_switch_publishes_it_everywhere(self):
        """A spec staged across every bike must not need a visit per bike."""
        adm = self.as_("admin")
        _, b = adm.post("/api/admin/fields", {
            "label": "Publish In One Go", "category": "Controls",
            "universal": True, "offline": True})
        key = b["field_key"]

        s, r = adm.post(f"/api/admin/fields/{key}/visibility", {"online": True})
        self.assertEqual(s, 200)
        self.assertTrue(r["online"])
        self.assertEqual(r["changed"], b["rows_added"])

        _, specs = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        labels = [x["label"] for c in specs["categories"] for x in c["specs"]]
        self.assertIn("Publish In One Go", labels)

        # and back off again
        s, r = adm.post(f"/api/admin/fields/{key}/visibility", {"online": False})
        self.assertEqual(r["changed"], b["rows_added"])
        _, after = self.anon().get(f"/api/bikes/{self.cb919}/specs")
        alabels = [x["label"] for c in after["categories"] for x in c["specs"]]
        self.assertNotIn("Publish In One Go", alabels)

    def test_96d_publishing_twice_changes_nothing_the_second_time(self):
        adm = self.as_("admin")
        _, b = adm.post("/api/admin/fields", {
            "label": "Idempotent Publish", "category": "Controls",
            "universal": True, "offline": True})
        key = b["field_key"]
        _, first = adm.post(f"/api/admin/fields/{key}/visibility", {"online": True})
        _, second = adm.post(f"/api/admin/fields/{key}/visibility", {"online": True})
        self.assertGreater(first["changed"], 0)
        self.assertEqual(second["changed"], 0)

    def test_96e_the_spec_tree_reports_what_is_offline(self):
        adm = self.as_("admin")
        _, b = adm.post("/api/admin/fields", {
            "label": "Counted As Offline", "category": "Controls",
            "universal": True, "offline": True})
        _, tree = adm.get("/api/spec-tree")
        made = [f for f in tree["fields"] if f["field_key"] == b["field_key"]][0]
        self.assertEqual(made["offline"], made["bikes"])

    def test_96f_only_admin_may_flip_a_whole_field(self):
        adm = self.as_("admin")
        _, b = adm.post("/api/admin/fields", {
            "label": "Admin Only Switch", "category": "Controls",
            "universal": True})
        key = b["field_key"]
        self.assertEqual(self.as_("m.alvarez").post(
            f"/api/admin/fields/{key}/visibility", {"online": False})[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/admin/fields/{key}/visibility", {"online": False})[0], 401)

    def test_96g_the_staging_and_the_switch_are_recorded(self):
        adm = self.as_("admin")
        _, b = adm.post("/api/admin/fields", {
            "label": "Audited Staging", "category": "Controls",
            "universal": True, "offline": True})
        adm.post(f"/api/admin/fields/{b['field_key']}/visibility", {"online": True})

        _, act = adm.get("/api/admin/activity")
        created = [a for a in act["actions"] if a["action"] == "field.create"
                   and "Audited Staging" in a["summary"]]
        flipped = [a for a in act["actions"] if a["action"] == "field.visibility"]
        self.assertTrue(created, "no create audit row")
        self.assertIn("offline", created[0]["summary"])
        self.assertTrue(flipped, "no visibility audit row")
        self.assertIn("online", flipped[0]["summary"])

    # -- wire colours as a value type ---------------------------------------
    #
    # Typed as text one wire arrives as "Yellow/Red", "yellow w/ red", "Y/R" and
    # "yel-red" from four riders, and none of those match, sort or draw. Stored
    # as colour keys and shown in each manufacturer's own shorthand.

    def _wire_spec(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        for c in specs["categories"]:
            for sp in c["specs"]:
                if sp["value_type"] == "wire_color":
                    return sp
        self.fail("no wire colour spec on this bike")

    def test_98a_the_value_type_reaches_the_page(self):
        sp = self._wire_spec(self.anon(), self.cb919)
        self.assertEqual(sp["value_type"], "wire_color")

    def test_98b_free_text_is_refused_on_a_wire_field(self):
        """The whole point: there is one way to say a colour."""
        mgr = self.as_("m.alvarez")
        sp = self._wire_spec(mgr, self.cb919)
        for junk in ("yellow w/ red stripe", "Y/R", "chartreuse", "puce/red"):
            s, b = mgr.patch(f"/api/specs/{sp['id']}", {"value": junk})
            self.assertEqual(s, 400, f"accepted {junk!r}")
            self.assertIn("not a colour", b["error"])

    def test_98c_what_riders_already_typed_is_understood(self):
        """"Yellow/Red" was entered before this existed. Adopting it beats
        making somebody re-enter a value they already sourced."""
        self.assertEqual(wire_colors.normalise("Yellow/Red"), "yellow/red")
        self.assertEqual(wire_colors.normalise("  light blue / WHITE "),
                         "lightblue/white")
        self.assertEqual(wire_colors.normalise("Green"), "green")

    def test_98d_a_value_is_stored_canonically_however_it_arrives(self):
        mgr = self.as_("m.alvarez")
        sp = self._wire_spec(mgr, self.cb919)
        s, _ = mgr.patch(f"/api/specs/{sp['id']}", {"value": "Light Blue/White"})
        self.assertEqual(s, 200)

        con = sqlite3.connect(self.db)
        stored = con.execute("SELECT value FROM specs WHERE id=?",
                             (sp["id"],)).fetchone()[0]
        con.close()
        self.assertEqual(stored, "lightblue/white")

    def test_98e_the_same_value_reads_differently_per_manufacturer(self):
        """Honda prints Bu for blue, Kawasaki BL, Harley BE. One stored value,
        three labels — the rider is holding their own diagram."""
        self.assertEqual(wire_colors.abbreviate("blue/red", "Honda"), "Bu/R")
        self.assertEqual(wire_colors.abbreviate("blue/red", "Kawasaki"), "BL/R")
        self.assertEqual(wire_colors.abbreviate("blue/red", "Harley-Davidson"),
                         "BE/R")
        # an unknown make still renders, rather than failing
        self.assertTrue(wire_colors.abbreviate("blue/red", "Bultaco"))

    def test_98f_the_vocabulary_endpoint_serves_a_make(self):
        s, b = self.anon().get("/api/wire-colors?make=Honda")
        self.assertEqual(s, 200)
        blue = [c for c in b["colors"] if c["key"] == "blue"][0]
        self.assertEqual(blue["abbr"], "Bu")
        self.assertTrue(blue["hex"].startswith("#"))

    def test_98f2_codes_are_what_each_factory_prints(self):
        """Read off the manufacturers' own diagram legends: Honda prints Bl
        for black (not B), Aprilia's V is GREEN and its G is yellow, KTM is
        lower case, Derbi's G is gray. A colour a legend does not define is
        shown by its plain name, never by a code invented for it, and a make
        with no legend on file shows plain names throughout."""
        self.assertEqual(wire_colors.abbreviate("black/white", "Honda"), "Bl/W")
        self.assertEqual(wire_colors.abbreviate("green/yellow", "Aprilia"), "V/G")
        self.assertEqual(wire_colors.abbreviate("black/red", "Vespa"), "Ne/Rs")
        self.assertEqual(wire_colors.abbreviate("red/white", "KTM"), "re/wh")
        self.assertEqual(wire_colors.abbreviate("gray/green", "Derbi"), "G/GR")
        self.assertEqual(wire_colors.abbreviate("blue/brown", "Triumph"), "U/N")
        self.assertEqual(wire_colors.abbreviate("lightblue", "Harley-Davidson"), "LBE")
        self.assertEqual(wire_colors.abbreviate("purple", "Honda"), "Purple")
        self.assertEqual(wire_colors.abbreviate("black/white", "Victory"), "Black/White")
        s, b = self.anon().get("/api/wire-colors?make=Ducati")
        self.assertEqual(s, 200)
        self.assertTrue(b["legend"])
        self.assertEqual([c["abbr"] for c in b["colors"] if c["key"] == "black"], ["Bk"])
        s, b = self.anon().get("/api/wire-colors?make=Indian")
        self.assertFalse(b["legend"])

    def test_98g_a_stripe_cannot_repeat_its_own_jacket(self):
        """Invisible on the bike, meaningless in the value."""
        with self.assertRaises(wire_colors.WireColorError):
            wire_colors.normalise("red/red")

    def test_98h_too_many_stripes_are_refused(self):
        with self.assertRaises(wire_colors.WireColorError):
            wire_colors.normalise("red/white/blue/green")

    def test_98i_rider_submissions_are_validated_too(self):
        """Every door that writes a value, not just the manager's."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "WIRE SUBMISSION TEST", 1998, 1998)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": self.CB750_ANSWERS})
        sp = self._wire_spec(adm, bike)

        rider = self.as_("t.moreno")
        if sp["value"]:
            s, _ = rider.post(f"/api/specs/{sp['id']}/alternates",
                              {"text": "not a colour"})
        else:
            s, _ = rider.post(f"/api/specs/{sp['id']}/value",
                              {"value": "not a colour"})
        self.assertEqual(s, 400, "a rider could still type free text")

    def test_98j_ordinary_fields_are_untouched(self):
        """Only wire fields are constrained. A part number is still free text."""
        mgr = self.as_("m.alvarez")
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        text_spec = [x for c in specs["categories"] for x in c["specs"]
                     if x["value_type"] == "text" and x["spec_type"] != "fixed"][0]
        s, _ = mgr.patch(f"/api/specs/{text_spec['id']}",
                         {"value": "Yuasa YTX12-BS"})
        self.assertEqual(s, 200)

    # -- a spec can be several wires ----------------------------------------
    #
    # A kickstand switch has two. Recorded one each they became two competing
    # "alternates" on this very database, when both are correct and neither
    # replaces the other.

    def test_99a_a_value_can_hold_several_wires(self):
        mgr = self.as_("m.alvarez")
        sp = self._wire_spec(mgr, self.cb919)
        s, _ = mgr.patch(f"/api/specs/{sp['id']}",
                         {"value": "green/white, green/black"})
        self.assertEqual(s, 200)

        con = sqlite3.connect(self.db)
        stored = con.execute("SELECT value FROM specs WHERE id=?",
                             (sp["id"],)).fetchone()[0]
        con.close()
        self.assertEqual(stored, "green/white, green/black")

    def test_99b_each_wire_can_say_what_it_goes_to(self):
        """Two similar wires need telling apart, and past two it is the only
        way to know which is which."""
        mgr = self.as_("m.alvarez")
        sp = self._wire_spec(mgr, self.cb919)
        s, _ = mgr.patch(f"/api/specs/{sp['id']}",
                         {"value": "to switch: green/white, to harness: green/black"})
        self.assertEqual(s, 200)

        parsed = wire_colors.parse_set(
            "to switch: green/white, to harness: green/black")
        self.assertEqual([r for r, _ in parsed], ["to switch", "to harness"])
        self.assertEqual(parsed[0][1], ["green", "white"])

    def test_99c_single_wire_values_are_unchanged(self):
        """Everything written before this must still mean what it meant."""
        for v in ("yellow/red", "lightblue/white", "green"):
            self.assertEqual(wire_colors.normalise_set(v), v)

    def test_99d_a_wire_set_is_described_per_manufacturer(self):
        out = wire_colors.describe_set("to switch: green/white, blue", "Honda")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["role"], "to switch")
        self.assertEqual(out[0]["abbr"], "G/W")
        self.assertEqual(out[1]["abbr"], "Bu")
        self.assertIsNone(out[1]["role"])

    def test_99e_the_set_is_bounded_and_roles_unique(self):
        with self.assertRaises(wire_colors.WireColorError):
            wire_colors.normalise_set(", ".join(["red"] * 9))
        with self.assertRaises(wire_colors.WireColorError):
            wire_colors.normalise_set("a: red, a: blue")
        with self.assertRaises(wire_colors.WireColorError):
            wire_colors.normalise_set("x" * 41 + ": red")

    def test_99f_a_bad_wire_anywhere_in_the_set_is_refused(self):
        """The second wire is checked as hard as the first."""
        mgr = self.as_("m.alvarez")
        sp = self._wire_spec(mgr, self.cb919)
        s, b = mgr.patch(f"/api/specs/{sp['id']}",
                         {"value": "green/white, chartreuse"})
        self.assertEqual(s, 400)
        self.assertIn("not a colour", b["error"])

    def test_99g_riders_can_submit_a_multi_wire_value(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "MULTI WIRE SUBMISSION", 1999, 1999)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": self.CB750_ANSWERS})
        sp = self._wire_spec(adm, bike)
        rider = self.as_("t.moreno")
        if sp["value"]:
            s, _ = rider.post(f"/api/specs/{sp['id']}/alternates",
                              {"text": "green/white, green/black"})
        else:
            s, _ = rider.post(f"/api/specs/{sp['id']}/value",
                              {"value": "green/white, green/black"})
        self.assertEqual(s, 200)

    # -- what a manager can do about a flag ---------------------------------
    #
    # A flag against an ALTERNATE left nothing to do but dismiss, which records
    # "not valid" about a flag that was valid — the complaint was about the
    # alternate, and the stock value was never what it was aimed at.

    def _open_flag(self, client, alternate=False):
        _, f = client.get("/api/manager/flags")
        for x in f["flags"]:
            if bool(x["alternate_id"]) == alternate:
                return x
        return None

    def test_a01_a_flag_on_an_alternate_can_remove_it(self):
        mgr = self.as_("m.alvarez")
        flag = self._open_flag(mgr, alternate=True)
        if not flag:
            self.skipTest("no open flag against an alternate")

        s, b = mgr.post(f"/api/flags/{flag['id']}/remove-alternate")
        self.assertEqual(s, 200)

        con = sqlite3.connect(self.db)
        paused, status = con.execute(
            "SELECT a.paused, v.status FROM spec_alternates a"
            " JOIN value_flags v ON v.alternate_id = a.id WHERE v.id=?",
            (flag["id"],)).fetchone()
        con.close()
        self.assertEqual(paused, 1, "the alternate is still showing")
        self.assertEqual(status, "fixed", "the flag was not closed")

    def test_a02_removing_an_alternate_keeps_its_text(self):
        """Hidden, not deleted — the call may turn out to be wrong.

        Makes its own alternate and flag rather than hunting for one: a test
        that skips when the fixture happens to be empty proves nothing.
        """
        mgr = self.as_("m.alvarez")
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        target = next(x for c in specs["categories"] for x in c["specs"]
                      if x["spec_type"] != "fixed" and x["value_type"] == "text"
                      and not x["paused"])

        rider = self.as_("t.moreno")
        s, alt = rider.post(f"/api/specs/{target['id']}/alternates",
                            {"text": "Doomed Alternate 12345"})
        self.assertEqual(s, 200)
        # An alternate is flagged through its own route; the spec route flags
        # the stock value.
        s, _ = rider.post(f"/api/alternates/{alt['id']}/flag",
                          {"reason": "incorrect"})
        self.assertEqual(s, 200)

        flag = next(x for x in mgr.get("/api/manager/flags")[1]["flags"]
                    if x["alternate_id"] == alt["id"])
        s, b = mgr.post(f"/api/flags/{flag['id']}/remove-alternate")
        self.assertEqual(s, 200)
        self.assertEqual(b["hidden"], "Doomed Alternate 12345")

        con = sqlite3.connect(self.db)
        row = con.execute("SELECT text, paused FROM spec_alternates WHERE id=?",
                          (alt["id"],)).fetchone()
        con.close()
        self.assertIsNotNone(row, "the alternate row was destroyed")
        self.assertEqual(row[0], "Doomed Alternate 12345", "the text was lost")
        self.assertEqual(row[1], 1, "it is still visible")

    def test_a03_the_two_fix_actions_do_not_cross_over(self):
        mgr = self.as_("m.alvarez")
        alt = self._open_flag(mgr, alternate=True)
        stock = self._open_flag(mgr, alternate=False)
        if alt:
            s, b = mgr.post(f"/api/flags/{alt['id']}/fix", {"new_value": "x"})
            self.assertEqual(s, 400)
            self.assertIn("against an alternate", b["error"])
        if stock:
            s, b = mgr.post(f"/api/flags/{stock['id']}/remove-alternate")
            self.assertEqual(s, 400)
            self.assertIn("not an alternate", b["error"])

    def test_a04_a_flagged_spec_can_go_offline_from_the_queue(self):
        """A flag saying the value is wrong is exactly when it should stop
        being read as fact."""
        mgr = self.as_("m.alvarez")
        flag = self._open_flag(mgr, alternate=False)
        if not flag:
            self.skipTest("no open flag on a stock value")

        s, b = mgr.post(f"/api/flags/{flag['id']}/offline", {"offline": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["offline"])

        con = sqlite3.connect(self.db)
        paused, status = con.execute(
            "SELECT s.paused, v.status FROM specs s"
            " JOIN value_flags v ON v.spec_id = s.id WHERE v.id=?",
            (flag["id"],)).fetchone()
        con.close()
        self.assertEqual(paused, 1)
        # Going offline buys time to check; it does not decide anything.
        self.assertEqual(status, "open", "the flag was closed by going offline")

        mgr.post(f"/api/flags/{flag['id']}/offline", {"offline": False})

    def test_a05_the_queue_says_whether_a_spec_is_offline(self):
        mgr = self.as_("m.alvarez")
        flag = self._open_flag(mgr, alternate=False)
        if not flag:
            self.skipTest("no open flag on a stock value")
        mgr.post(f"/api/flags/{flag['id']}/offline", {"offline": True})

        _, f = mgr.get("/api/manager/flags")
        row = [x for x in f["flags"] if x["id"] == flag["id"]][0]
        self.assertTrue(row["spec_offline"])
        self.assertIn("value_type", row)

        mgr.post(f"/api/flags/{flag['id']}/offline", {"offline": False})

    def test_a06_a_manager_can_write_to_an_admin_or_the_flagger(self):
        mgr = self.as_("m.alvarez")
        flag = self._open_flag(mgr, alternate=False)
        if not flag:
            self.skipTest("no open flag")

        s, b = mgr.post(f"/api/flags/{flag['id']}/messages",
                        {"body": "which production run?", "to": "flagger"})
        self.assertEqual(s, 200)
        s, b = mgr.post(f"/api/flags/{flag['id']}/messages",
                        {"body": "this field is wrong platform-wide", "to": "admin"})
        self.assertEqual(s, 200)
        self.assertEqual(b["to"], "admin")

        con = sqlite3.connect(self.db)
        roles = [r[0] for r in con.execute(
            "SELECT u.role FROM flag_messages m JOIN users u ON u.id = m.to_user"
            " WHERE m.value_flag_id=? ORDER BY m.id", (flag["id"],))]
        con.close()
        self.assertIn("admin", roles, "the admin message did not reach an admin")

    def test_a07_an_unknown_recipient_is_refused(self):
        mgr = self.as_("m.alvarez")
        flag = self._open_flag(mgr, alternate=False)
        if not flag:
            self.skipTest("no open flag")
        s, _ = mgr.post(f"/api/flags/{flag['id']}/messages",
                        {"body": "hello", "to": "everyone"})
        self.assertEqual(s, 400)

    def test_a08_only_this_bike_s_manager_may_act_on_its_flags(self):
        adm = self.as_("admin")
        flag = self._open_flag(adm, alternate=False)
        if not flag:
            self.skipTest("no open flag")
        rider = self.as_("t.moreno")
        self.assertEqual(rider.post(f"/api/flags/{flag['id']}/offline",
                                    {"offline": True})[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/flags/{flag['id']}/remove-alternate")[0], 401)

    # -- one spec, different values for different years ---------------------
    #
    # A bike whose tank grew in 1978 is the same machine. Splitting the BIKE
    # would duplicate the specs that did not change to vary the two that did,
    # and lean on sibling-divergence to notice when the duplicates drift.
    # Splitting the SPEC keeps the duplication where the difference is.

    _span_seq = 0

    def _span_bike(self, adm):
        """A bike with a multi-year span and a spec to split.

        Its own bike per test: model_code is unique per make and start year, so
        a shared one makes every test after the first collide.
        """
        ApiTest._span_seq += 1
        bike = self._new_bike(adm, f"YEAR SPLIT TEST {ApiTest._span_seq}", 1975, 1985)
        adm.post(f"/api/questionnaire/{bike}/build",
                 {"answers": dict(self.CB750_ANSWERS)})
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        spec = next(x for c in specs["categories"] for x in c["specs"]
                    if x["value_type"] == "text")
        return bike, spec

    def test_b01_a_spec_can_be_split_at_a_year(self):
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        s, b = adm.post(f"/api/specs/{spec['id']}/split-year", {"at_year": 1978})
        self.assertEqual(s, 200)
        self.assertEqual((b["earlier"]["year_from"], b["earlier"]["year_to"]),
                         (1975, 1977))
        self.assertEqual((b["later"]["year_from"], b["later"]["year_to"]),
                         (1978, 1985))

        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        fam = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == spec["field_key"]]
        self.assertEqual(len(fam), 2)
        self.assertTrue(all(x["variant_count"] == 2 for x in fam))

    def test_b02_the_bike_is_not_split(self):
        """The whole point: one bike, one page, one set of shared specs."""
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        con = sqlite3.connect(self.db)
        before = con.execute("SELECT COUNT(*) FROM bikes").fetchone()[0]
        con.close()

        adm.post(f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})

        con = sqlite3.connect(self.db)
        after = con.execute("SELECT COUNT(*) FROM bikes").fetchone()[0]
        split = con.execute("SELECT COUNT(*) FROM bikes"
                            " WHERE split_from_bike_id IS NOT NULL").fetchone()[0]
        con.close()
        self.assertEqual(before, after, "a bike was created")
        self.assertEqual(split, 0, "the bike was split")

    def test_b03_each_range_holds_its_own_value(self):
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        _, b = adm.post(f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})
        adm.patch(f"/api/specs/{b['earlier']['id']}", {"value": "early value"})
        adm.patch(f"/api/specs/{b['later']['id']}", {"value": "late value"})

        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        fam = {x["year_from"]: x["value"] for c in specs["categories"]
               for x in c["specs"] if x["field_key"] == spec["field_key"]}
        self.assertEqual(fam[1975], "early value")
        self.assertEqual(fam[1980], "late value")

    def test_b04_the_year_view_resolves_each_model_year(self):
        """year_specs expands a bike across its years; a ranged spec must only
        appear on the years it covers, and never twice on one year."""
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        _, b = adm.post(f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})
        adm.patch(f"/api/specs/{b['earlier']['id']}", {"value": "EARLY"})
        adm.patch(f"/api/specs/{b['later']['id']}", {"value": "LATE"})

        con = sqlite3.connect(self.db)
        rows = con.execute(
            "SELECT year, value FROM year_specs WHERE bike_id=? AND field_key=?"
            " ORDER BY year", (bike, spec["field_key"])).fetchall()
        con.close()
        by_year = dict(rows)
        self.assertEqual(by_year[1975], "EARLY")
        self.assertEqual(by_year[1979], "EARLY")
        self.assertEqual(by_year[1980], "LATE")
        self.assertEqual(by_year[1985], "LATE")
        self.assertEqual(len(rows), 11, "a year got two values or none")

    def test_b05_a_split_survives_re_answering_the_questionnaire(self):
        """Everything that adds fields in bulk says INSERT OR IGNORE. Once a
        field is split there is no unranged row to collide with, so without the
        guard those inserts would add a third row covering every year."""
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        adm.post(f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})

        s, _ = adm.post(f"/api/questionnaire/{bike}/build",
                        {"answers": dict(self.CB750_ANSWERS)})
        self.assertEqual(s, 200)

        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM specs WHERE bike_id=? AND field_key=?",
                        (bike, spec["field_key"])).fetchone()[0]
        unranged = con.execute(
            "SELECT COUNT(*) FROM specs WHERE bike_id=? AND field_key=?"
            " AND year_from IS NULL", (bike, spec["field_key"])).fetchone()[0]
        con.close()
        self.assertEqual(n, 2, "a third row appeared")
        self.assertEqual(unranged, 0,
                         "an every-year row appeared beside the variants")

    def test_b06_nonsense_split_years_are_refused(self):
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        for bad in (1975, 1974, 1986, 2500, "soon", None):
            s, _ = adm.post(f"/api/specs/{spec['id']}/split-year",
                            {"at_year": bad})
            self.assertEqual(s, 400, f"accepted at_year={bad!r}")

    def test_b07_merging_needs_confirming_and_says_what_goes(self):
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        _, b = adm.post(f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})
        adm.patch(f"/api/specs/{b['earlier']['id']}", {"value": "EARLY"})
        adm.patch(f"/api/specs/{b['later']['id']}", {"value": "LATE"})

        s, body = adm.post(f"/api/specs/{b['later']['id']}/merge-years")
        self.assertEqual(s, 409)

        s, body = adm.post(f"/api/specs/{b['later']['id']}/merge-years",
                           {"force": True})
        self.assertEqual(s, 200)
        self.assertEqual(body["kept"], "LATE")
        self.assertEqual([d["value"] for d in body["dropped"]], ["EARLY"])

        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        fam = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == spec["field_key"]]
        self.assertEqual(len(fam), 1)
        self.assertIsNone(fam[0]["year_from"])
        self.assertEqual(fam[0]["value"], "LATE")

    def test_b08_only_this_bike_s_manager_may_split_a_spec(self):
        adm = self.as_("admin")
        bike, spec = self._span_bike(adm)
        self.assertEqual(self.as_("t.moreno").post(
            f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/specs/{spec['id']}/split-year", {"at_year": 1980})[0], 401)

    def test_b09_unsplit_specs_are_untouched(self):
        """The ordinary case stays exactly as it was: one row, no range."""
        adm = self.as_("admin")
        _, specs = adm.get(f"/api/bikes/{self.cb919}/specs")
        rows = [x for c in specs["categories"] for x in c["specs"]]
        self.assertTrue(rows)
        self.assertTrue(all(x["year_from"] is None for x in rows))
        self.assertTrue(all(x["variant_count"] == 1 for x in rows))

    # -- a pinned spec can replace its section rather than duplicate it ------

    def _pinnable_spec(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        for c in specs["categories"]:
            if c["name"] == "General":
                continue
            for sp in c["specs"]:
                if not sp["in_header"]:
                    return sp
        self.fail("nothing left to pin on this bike")

    def test_c01_pinning_still_defaults_to_both_places(self):
        """The default is unchanged: the header is a summary, and the spec
        keeps its row where it can be voted on."""
        mgr = self.as_("m.alvarez")
        sp = self._pinnable_spec(mgr, self.cb919)
        s, b = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": sp["field_key"]})
        self.assertEqual(s, 200)
        self.assertFalse(b["hide_below"])

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        row = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == sp["field_key"]][0]
        self.assertTrue(row["in_header"])
        self.assertFalse(row["header_only"])
        mgr.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")

    def test_c02_a_spec_can_be_pinned_header_only(self):
        mgr = self.as_("m.alvarez")
        sp = self._pinnable_spec(mgr, self.cb919)
        s, b = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": sp["field_key"], "hide_below": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["hide_below"])

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        row = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == sp["field_key"]][0]
        self.assertTrue(row["header_only"])
        mgr.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")

    def test_c03_the_choice_can_be_changed_without_unpinning(self):
        mgr = self.as_("m.alvarez")
        sp = self._pinnable_spec(mgr, self.cb919)
        mgr.post(f"/api/bikes/{self.cb919}/header",
                 {"field_key": sp["field_key"], "hide_below": True})
        s, b = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": sp["field_key"], "hide_below": False})
        self.assertEqual(s, 200)
        self.assertFalse(b["hide_below"], "re-pinning did not change the choice")

        con = sqlite3.connect(self.db)
        n = con.execute("SELECT COUNT(*) FROM bike_header_specs"
                        " WHERE bike_id=? AND field_key=?",
                        (self.cb919, sp["field_key"])).fetchone()[0]
        con.close()
        self.assertEqual(n, 1, "re-pinning duplicated the pin")
        mgr.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")

    def test_c04_unpinning_clears_the_choice_too(self):
        mgr = self.as_("m.alvarez")
        sp = self._pinnable_spec(mgr, self.cb919)
        mgr.post(f"/api/bikes/{self.cb919}/header",
                 {"field_key": sp["field_key"], "hide_below": True})
        mgr.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        row = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == sp["field_key"]][0]
        self.assertFalse(row["in_header"])
        self.assertFalse(row["header_only"])

    def test_c05_only_this_bike_s_manager_may_choose(self):
        adm = self.as_("admin")
        sp = self._pinnable_spec(adm, self.cb919)
        body = {"field_key": sp["field_key"], "hide_below": True}
        self.assertEqual(self.as_("t.moreno").post(
            f"/api/bikes/{self.cb919}/header", body)[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/bikes/{self.cb919}/header", body)[0], 401)

    # -- a General field can be taken out of ONE bike's header --------------
    #
    # General fields are in every header by Spec Tree rule, and pinning was
    # built to add to that and never subtract. So the CB919's manager could
    # see Wheelbase in the header and had nothing to click.

    def _general_spec(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        gen = [c for c in specs["categories"] if c["name"] == "General"]
        if not gen or not gen[0]["specs"]:
            self.skipTest("no General spec on this bike")
        return gen[0]["specs"][0]

    def test_d01_manager_hides_a_general_field_from_their_header(self):
        mgr = self.as_("m.alvarez")
        sp = self._general_spec(mgr, self.cb919)
        s, b = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": sp["field_key"], "hidden": True})
        self.assertEqual(s, 200)
        self.assertTrue(b["hidden"])

        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        row = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == sp["field_key"]][0]
        self.assertTrue(row["header_hidden"])
        self.assertFalse(row["in_header"])
        self.assertEqual(row["category"], "General")
        mgr.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")

    def test_d02_hiding_is_per_bike(self):
        """Wheelbase off the CB919's header says nothing about the other 261."""
        adm = self.as_("admin")
        sp = self._general_spec(adm, self.cb919)
        other = self._new_bike(adm, "GENERAL HIDE SCOPE", 1996, 1996)
        adm.post(f"/api/questionnaire/{other}/build",
                 {"answers": self.CB750_ANSWERS})
        adm.post(f"/api/bikes/{self.cb919}/header",
                 {"field_key": sp["field_key"], "hidden": True})

        _, specs = adm.get(f"/api/bikes/{other}/specs")
        same = [x for c in specs["categories"] for x in c["specs"]
                if x["field_key"] == sp["field_key"]]
        for x in same:
            self.assertFalse(x["header_hidden"], "hiding on one bike hid another")
        adm.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")

    def test_d03_restoring_puts_it_back(self):
        mgr = self.as_("m.alvarez")
        sp = self._general_spec(mgr, self.cb919)
        mgr.post(f"/api/bikes/{self.cb919}/header",
                 {"field_key": sp["field_key"], "hidden": True})
        s, _ = mgr.delete(f"/api/bikes/{self.cb919}/header/{sp['field_key']}")
        self.assertEqual(s, 200)
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        row = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == sp["field_key"]][0]
        self.assertFalse(row["header_hidden"])

    def test_d04_a_general_field_still_cannot_be_pinned(self):
        """Pinning it would be a no-op; the error says what IS possible."""
        mgr = self.as_("m.alvarez")
        sp = self._general_spec(mgr, self.cb919)
        s, b = mgr.post(f"/api/bikes/{self.cb919}/header",
                        {"field_key": sp["field_key"]})
        self.assertEqual(s, 409)
        self.assertIn("hidden: true", b["error"])

    def test_d05_hidden_and_pinned_are_distinct_states(self):
        mgr = self.as_("m.alvarez")
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        non_gen = next(x for c in specs["categories"] for x in c["specs"]
                       if c["name"] != "General" and not x["in_header"])
        mgr.post(f"/api/bikes/{self.cb919}/header",
                 {"field_key": non_gen["field_key"]})
        _, specs = mgr.get(f"/api/bikes/{self.cb919}/specs")
        row = [x for c in specs["categories"] for x in c["specs"]
               if x["field_key"] == non_gen["field_key"]][0]
        self.assertTrue(row["in_header"])
        self.assertFalse(row["header_hidden"])
        mgr.delete(f"/api/bikes/{self.cb919}/header/{non_gen['field_key']}")

    def test_d06_only_this_bike_s_manager_may_hide(self):
        adm = self.as_("admin")
        sp = self._general_spec(adm, self.cb919)
        body = {"field_key": sp["field_key"], "hidden": True}
        self.assertEqual(self.as_("t.moreno").post(
            f"/api/bikes/{self.cb919}/header", body)[0], 403)
        self.assertEqual(self.anon().post(
            f"/api/bikes/{self.cb919}/header", body)[0], 401)

    # -- minimum fuel octane and max ethanol -------------------------------
    #
    # One octane field: the region is in the value. Stored as the grade the
    # manual named, in the scale the manual used; the other scale is an
    # equivalence looked up from published pairings, never computed, because
    # AKI cannot be derived from RON without MON and no manual publishes MON.

    def _fuel_specs(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        rows = [x for c in specs["categories"] for x in c["specs"]]
        oct = next((x for x in rows if x["field_key"] == "fuel_octane_grade"), None)
        eth = next((x for x in rows if x["field_key"] == "max_ethanol"), None)
        if not oct or not eth:
            self.skipTest("bike has no fuel fields")
        return oct, eth

    def test_e01_the_fields_are_typed_and_named(self):
        oct, eth = self._fuel_specs(self.anon(), self.cb919)
        self.assertEqual(oct["label"], "Minimum Fuel Octane")
        self.assertEqual(oct["value_type"], "fuel_octane")
        self.assertEqual(eth["label"], "Max Ethanol")
        self.assertEqual(eth["value_type"], "ethanol")

    def test_e02_the_key_did_not_change(self):
        """Decision 1: header pins and specs point at fuel_octane_grade."""
        oct, _ = self._fuel_specs(self.anon(), self.cb919)
        self.assertEqual(oct["field_key"], "fuel_octane_grade")

    def test_e03_stored_as_scale_and_grade(self):
        """Decision 2: 'AKI:91', not a row id and not a converted figure."""
        mgr = self.as_("m.alvarez")
        oct, _ = self._fuel_specs(mgr, self.cb919)
        for typed, want in (("91 AKI", "AKI:91"), ("ron 95", "RON:95"),
                            ("87", "AKI:87"), ("aki:93", "AKI:93")):
            s, _ = mgr.patch(f"/api/specs/{oct['id']}", {"value": typed})
            self.assertEqual(s, 200, f"refused {typed!r}")
            con = sqlite3.connect(self.db)
            stored = con.execute("SELECT value FROM specs WHERE id=?",
                                 (oct["id"],)).fetchone()[0]
            con.close()
            self.assertEqual(stored, want)

    def test_e04_free_text_and_off_list_grades_are_refused(self):
        mgr = self.as_("m.alvarez")
        oct, eth = self._fuel_specs(mgr, self.cb919)
        for junk in ("premium", "91 or better", "AKI:90", "RON:96", "97"):
            s, _ = mgr.patch(f"/api/specs/{oct['id']}", {"value": junk})
            self.assertEqual(s, 400, f"accepted {junk!r} as an octane grade")
        for junk in ("E20", "some", "10%"):
            s, _ = mgr.patch(f"/api/specs/{eth['id']}", {"value": junk})
            self.assertEqual(s, 400, f"accepted {junk!r} as an ethanol limit")

    def test_e05_a_bare_number_in_both_scales_is_refused(self):
        """91 is a grade in AKI and in RON. '91' alone cannot be stored as
        either without guessing."""
        mgr = self.as_("m.alvarez")
        oct, _ = self._fuel_specs(mgr, self.cb919)
        s, b = mgr.patch(f"/api/specs/{oct['id']}", {"value": "91"})
        self.assertEqual(s, 400)
        self.assertIn("both AKI and RON", b["error"])

    def test_e06_no_converter_only_published_pairings(self):
        """The important correction. 95 RON is a published RANGE and must not
        collapse; a reverse lookup that mixes published and formula rows is
        labelled formula, the conservative way round."""
        import fuel_octane as fo
        e = fo.describe_octane("RON:95")["equivalent"]
        self.assertEqual((e["min"], e["max"], e["method"]), (90, 91, "published"))
        e = fo.describe_octane("RON:98")["equivalent"]
        self.assertEqual((e["min"], e["max"], e["method"]), (93, 93, "published"))
        e = fo.describe_octane("AKI:91")["equivalent"]
        self.assertEqual((e["min"], e["max"]), (95, 96))
        self.assertEqual(e["method"], "formula", "a mixed lookup must be labelled formula")
        e = fo.describe_octane("AKI:93")["equivalent"]
        self.assertEqual((e["min"], e["max"], e["method"]), (98, 98, "published"))

    def test_e07_ethanol_normalises_and_is_its_own_field(self):
        """Decision 3: separate field, opposite shape."""
        mgr = self.as_("m.alvarez")
        oct, eth = self._fuel_specs(mgr, self.cb919)
        self.assertNotEqual(oct["id"], eth["id"])
        for typed, want in (("e10", "E10"), ("ethanol free", "E0"), ("0", "E0"), ("E5", "E5")):
            s, _ = mgr.patch(f"/api/specs/{eth['id']}", {"value": typed})
            self.assertEqual(s, 200, f"refused {typed!r}")
            con = sqlite3.connect(self.db)
            stored = con.execute("SELECT value FROM specs WHERE id=?", (eth["id"],)).fetchone()[0]
            con.close()
            self.assertEqual(stored, want)

    def test_e08_an_alternate_below_the_minimum_is_allowed(self):
        """Decision 5."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "OCTANE ALT TEST", 1999, 1999)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})
        oct, _ = self._fuel_specs(adm, bike)
        adm.patch(f"/api/specs/{oct['id']}", {"value": "AKI:91"})
        rider = self.as_("t.moreno")
        s, _ = rider.post(f"/api/specs/{oct['id']}/alternates", {"text": "AKI:87"})
        self.assertEqual(s, 200, "an alternate below the stated minimum was refused")

    def test_e09_every_bike_gets_both_fields_without_being_asked(self):
        """Every machine in the catalog runs on gasoline, so there is no
        fuel-type question: the two fields are universal and arrive at bike
        creation, before any questionnaire is run."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FUEL UNIVERSAL TEST", 1999, 1999)
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        keys = {x["field_key"] for c in specs["categories"] for x in c["specs"]}
        self.assertIn("fuel_octane_grade", keys, "octane did not land at creation")
        self.assertIn("max_ethanol", keys, "ethanol did not land at creation")

        # the questionnaire adds to that, and never takes it away
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})
        _, specs = adm.get(f"/api/bikes/{bike}/specs")
        keys = {x["field_key"] for c in specs["categories"] for x in c["specs"]}
        self.assertIn("fuel_octane_grade", keys)
        self.assertIn("max_ethanol", keys)

    def test_e10_the_fuel_fields_are_universal_and_no_question_names_them(self):
        con = sqlite3.connect(self.db)
        uni = dict(con.execute(
            "SELECT field_key, universal FROM spec_fields"
            " WHERE field_key IN ('fuel_octane_grade','max_ethanol')").fetchall())
        trig = con.execute(
            "SELECT COUNT(*) FROM field_triggers"
            " WHERE field_key IN ('fuel_octane_grade','max_ethanol')").fetchone()[0]
        con.close()
        self.assertEqual(uni, {"fuel_octane_grade": 1, "max_ethanol": 1})
        self.assertEqual(trig, 0, "a branch still gates a fuel field")
        import questionnaire as qn
        self.assertNotIn("q4b", qn.load()["questions"], "the fuel-type question is back")
        self.assertEqual(qn.questions_triggering("Minimum Fuel Octane"), [])

    def test_e11_the_vocabulary_endpoint(self):
        s, b = self.anon().get("/api/fuel-octane")
        self.assertEqual(s, 200)
        keys = {g["key"] for g in b["grades"]}
        self.assertIn("AKI:87", keys)
        self.assertIn("RON:95", keys)
        ron95 = next(g for g in b["grades"] if g["key"] == "RON:95")
        self.assertEqual(ron95["equivalent"]["text"], "90–91 AKI")
        self.assertEqual([e["key"] for e in b["ethanol"]], ["E0", "E5", "E10", "E15"])

    # -- a bike's identity: rename, and split at a model year ------------------
    #
    # Both open to the bike's manager; admin is told when a manager does it.

    def _identity_bike(self, adm, code, ys, ye, manager="cb919_dave"):
        bike = self._new_bike(adm, code, ys, ye)
        adm.post(f"/api/questionnaire/{bike}/build", {"answers": self.CB750_ANSWERS})
        con = sqlite3.connect(self.db)
        uid = con.execute("SELECT id FROM users WHERE username=?", (manager,)).fetchone()[0]
        con.close()
        s, b = adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": uid})
        self.assertIn(s, (200, 409), b)
        return bike

    def _flat_specs(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        return [x for c in specs["categories"] for x in c["specs"]]

    def _unseen_notices(self, adm):
        _, n = adm.get("/api/admin/notices")
        return n["items"]

    def test_f01_manager_renames_their_bike_and_admin_is_told(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "RENAME MGR TEST", 1990, 1992)
        mgr = self.as_("cb919_dave")
        s, b = mgr.patch(f"/api/bikes/{bike}/name", {
            "name": "Honda Rename Test 500",
            "other_names": [{"name": "RT500", "market": "us"}, {"name": "", "market": ""}]})
        self.assertEqual(s, 200, b)
        self.assertTrue(b["notified_admin"])
        _, page = self.anon().get(f"/api/bikes/{bike}")
        self.assertEqual(page["display_name"], "Honda Rename Test 500")
        self.assertEqual([(n["name"], n["market"]) for n in page["names"] if not n["is_primary"]],
                         [("RT500", "US")])
        notices = self._unseen_notices(adm)
        mine = [n for n in notices if n["bike_id"] == bike and n["kind"] == "rename"]
        self.assertEqual(len(mine), 1, notices)
        self.assertIn("cb919_dave", mine[0]["summary"])
        self.assertIn("Honda Rename Test 500", mine[0]["summary"])
        _, summary = adm.get("/api/admin/summary")
        self.assertGreaterEqual(summary["manager_changes"], 1)

        # seen -> gone from the queue and from the count
        s, r = adm.post("/api/admin/notices/seen", {"id": mine[0]["id"]})
        self.assertEqual(s, 200); self.assertEqual(r["seen"], 1)
        self.assertNotIn(mine[0]["id"], [n["id"] for n in self._unseen_notices(adm)])

    def test_f02_admin_rename_makes_no_notice_and_keys_are_untouched(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "RENAME ADM TEST", 1990, 1990)
        before = len(self._unseen_notices(adm))
        s, b = adm.patch(f"/api/bikes/{bike}/name", {"name": "Honda Admin Renamed"})
        self.assertEqual(s, 200, b)
        self.assertFalse(b["notified_admin"])
        self.assertEqual(len(self._unseen_notices(adm)), before)
        con = sqlite3.connect(self.db)
        make, code = con.execute("SELECT make, model_code FROM bikes WHERE id=?", (bike,)).fetchone()
        con.close()
        self.assertEqual((make, code), ("Honda", "RENAME ADM TEST"), "rename touched the identity keys")

    def test_f03_rename_is_refused_to_strangers_and_to_nonsense(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "RENAME DENY TEST", 1990, 1990)
        self.assertEqual(self.as_("t.moreno").patch(f"/api/bikes/{bike}/name", {"name": "X"})[0], 403)
        self.assertEqual(self.as_("m.alvarez").patch(f"/api/bikes/{bike}/name", {"name": "X"})[0], 403,
                         "a manager of a different bike renamed this one")
        self.assertEqual(self.anon().patch(f"/api/bikes/{bike}/name", {"name": "X"})[0], 401)
        mgr = self.as_("cb919_dave")
        self.assertEqual(mgr.patch(f"/api/bikes/{bike}/name", {"name": "   "})[0], 400)
        self.assertEqual(mgr.patch(f"/api/bikes/{bike}/name", {"name": "x" * 81})[0], 400)
        self.assertEqual(mgr.patch(f"/api/bikes/{bike}/name", {"name": "ok", "other_names": "no"})[0], 400)
        self.assertEqual(mgr.patch("/api/bikes/999999/name", {"name": "ok"})[0], 403)

    def test_f04_split_moves_the_later_years_and_copies_the_sheet(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "SPLIT TEST", 1975, 1989)
        specs = self._flat_specs(adm, bike)
        plain = next(x for x in specs if x["value_type"] == "text" and x["category"] != "General")
        adm.patch(f"/api/specs/{plain['id']}", {"value": "shared value"})
        rider = self.as_("t.moreno")
        rider.post(f"/api/specs/{plain['id']}/alternates", {"text": "an alternate"})
        # one spec year-scoped across the split: 1975-1979 / 1980-1989
        var = next(x for x in specs if x["value_type"] == "text" and x["id"] != plain["id"])
        adm.patch(f"/api/specs/{var['id']}", {"value": "early part"})
        s, sp = adm.post(f"/api/specs/{var['id']}/split-year", {"at_year": 1980})
        self.assertEqual(s, 200, sp)
        adm.patch(f"/api/specs/{sp['later']['id']}", {"value": "late part"})
        # a rider owns an '85
        _, page = adm.get(f"/api/bikes/{bike}")
        y85 = next(y["bike_year_id"] for y in page["years"] if y["year"] == 1985)
        s, g = rider.post("/api/garage", {"bike_year_id": y85})
        self.assertEqual(s, 200, g)
        pinned = adm.post(f"/api/bikes/{bike}/header", {"field_key": plain["field_key"]})
        n_specs = len(specs) + 1          # the year split added a row

        mgr = self.as_("cb919_dave")
        s, r = mgr.post(f"/api/bikes/{bike}/split", {"at_year": 1978, "name": "Honda Split Later"})
        self.assertEqual(s, 200, r)
        self.assertTrue(r["notified_admin"])
        later = r["later"]["bike_id"]
        self.assertEqual(r["earlier"]["year_range"], "1975-1977")
        self.assertEqual(r["later"]["year_range"], "1978-1989")
        self.assertEqual(r["later"]["display_name"], "Honda Split Later")
        self.assertEqual(r["model_years_moved"], 12)

        _, early_page = self.anon().get(f"/api/bikes/{bike}")
        _, later_page = self.anon().get(f"/api/bikes/{later}")
        self.assertEqual([y["year"] for y in early_page["years"]], list(range(1975, 1978)))
        self.assertEqual([y["year"] for y in later_page["years"]], list(range(1978, 1990)))
        self.assertEqual(later_page["split_from_bike_id"], bike)
        self.assertEqual(later_page["split_at_year"], 1978)
        self.assertTrue(later_page["years_verified"] and early_page["years_verified"])
        self.assertIn("cb919_dave", [m["username"] for m in later_page["managers"]])

        early = self._flat_specs(adm, bike)
        late = self._flat_specs(adm, later)
        self.assertEqual(len(late), n_specs, "the later bike did not get the whole sheet")
        e_plain = next(x for x in early if x["field_key"] == plain["field_key"])
        l_plain = next(x for x in late if x["field_key"] == plain["field_key"])
        self.assertEqual((e_plain["value"], l_plain["value"]), ("shared value", "shared value"))
        self.assertNotEqual(e_plain["id"], l_plain["id"])
        self.assertEqual([a["text"] for a in l_plain["alternates"]], ["an alternate"])
        self.assertTrue(l_plain["in_header"], "the header pin was not copied")

        # the year-scoped spec: 1975-1977 stays (and is whole-span, so unranged
        # again); 1978-1979 and 1980-1989 are on the later bike
        e_var = [x for x in early if x["field_key"] == var["field_key"]]
        l_var = sorted([x for x in late if x["field_key"] == var["field_key"]],
                       key=lambda x: x["year_from"])
        self.assertEqual([(x["value"], x["year_from"], x["year_to"]) for x in e_var],
                         [("early part", None, None)])
        self.assertEqual([(x["value"], x["year_from"], x["year_to"]) for x in l_var],
                         [("early part", 1978, 1979), ("late part", 1980, 1989)])

        # the rider's '85 followed the '85
        _, garage = rider.get("/api/garage")
        mine = next(g for g in garage["garage"] if g["bike_year_id"] == y85)
        self.assertEqual(mine["bike_id"], later)

        notices = [n for n in self._unseen_notices(adm) if n["bike_id"] == bike and n["kind"] == "split"]
        self.assertEqual(len(notices), 1)
        self.assertIn("1978", notices[0]["summary"])

    def test_f05_split_refuses_bad_years_strangers_and_repeats(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "SPLIT DENY TEST", 2000, 2004)
        self.assertEqual(self.as_("t.moreno").post(f"/api/bikes/{bike}/split", {"at_year": 2002})[0], 403)
        self.assertEqual(self.as_("m.alvarez").post(f"/api/bikes/{bike}/split", {"at_year": 2002})[0], 403)
        mgr = self.as_("cb919_dave")
        for bad in (2000, 1999, 2005, "soon"):
            s, b = mgr.post(f"/api/bikes/{bike}/split", {"at_year": bad})
            self.assertEqual(s, 400, f"{bad!r}: {b}")
        s, b = mgr.post(f"/api/bikes/{bike}/split", {"at_year": 2003})
        self.assertEqual(s, 200, b)
        s, b = mgr.post(f"/api/bikes/{bike}/split", {"at_year": 2003})
        self.assertEqual(s, 400, "the earlier bike now ends at 2002; 2003 is outside it")
        # the model years must not have been claimed twice
        con = sqlite3.connect(self.db)
        dup = con.execute(
            "SELECT year, COUNT(*) FROM bike_years y JOIN bikes b ON b.id=y.bike_id"
            " WHERE b.model_code='SPLIT DENY TEST' GROUP BY year HAVING COUNT(*)>1").fetchall()
        con.close()
        self.assertEqual(dup, [])

    def test_f06_a_single_year_bike_cannot_be_split(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "SPLIT ONE YEAR", 2010, 2010)
        s, b = adm.post(f"/api/bikes/{bike}/split", {"at_year": 2010})
        self.assertEqual(s, 400, b)

    # -- the bike's photo ----------------------------------------------------------

    PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

    def _raw_post(self, client, path, data, ctype):
        req = urllib.request.Request(self.base + path, data=data, method="POST")
        req.add_header("Content-Type", ctype)
        try:
            with client.opener.open(req) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def _raw_get(self, client, path):
        try:
            with client.opener.open(self.base + path) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type"), e.read()

    def test_g01_manager_uploads_a_photo_and_it_replaces_photo_pending(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "PHOTO TEST", 1999, 1999)
        mgr = self.as_("cb919_dave")
        _, before = self.anon().get(f"/api/bikes/{bike}")
        self.assertIsNone(before["photo"])

        s, b = self._raw_post(mgr, f"/api/bikes/{bike}/photo", self.PNG, "image/png")
        self.assertEqual(s, 200, b)
        self.assertTrue(b["notified_admin"])
        self.assertEqual(b["photo"]["mime"], "image/png")
        _, page = self.anon().get(f"/api/bikes/{bike}")
        self.assertEqual(page["photo"]["uploaded_by"], "cb919_dave")
        url = page["photo"]["url"]
        self.assertTrue(url.startswith(f"/photos/{bike}.png"), url)
        s, ctype, data = self._raw_get(self.anon(), url)
        self.assertEqual((s, ctype, data), (200, "image/png", self.PNG))

        notices = [n for n in self._unseen_notices(adm) if n["bike_id"] == bike and n["kind"] == "photo"]
        self.assertEqual(len(notices), 1)
        self.assertIn("cb919_dave added a photo", notices[0]["summary"])

        # a JPEG replaces it: one file, the right type, the old one gone
        s, b = self._raw_post(mgr, f"/api/bikes/{bike}/photo", self.JPG, "image/jpeg")
        self.assertEqual(s, 200, b)
        self.assertTrue(b["photo"]["url"].startswith(f"/photos/{bike}.jpg"))
        self.assertEqual(self._raw_get(self.anon(), f"/photos/{bike}.png")[0], 404)
        self.assertEqual(self._raw_get(self.anon(), f"/photos/{bike}.jpg")[1], "image/jpeg")

        # removed -> pending again
        s, _ = mgr.delete(f"/api/bikes/{bike}/photo")
        self.assertEqual(s, 200)
        _, page = self.anon().get(f"/api/bikes/{bike}")
        self.assertIsNone(page["photo"])
        self.assertEqual(self._raw_get(self.anon(), f"/photos/{bike}.jpg")[0], 404)
        self.assertEqual(mgr.delete(f"/api/bikes/{bike}/photo")[0], 404)

    def test_g02_the_bytes_decide_the_type_and_strangers_are_refused(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "PHOTO DENY TEST", 1999, 1999)
        mgr = self.as_("cb919_dave")
        # a GIF, an SVG and plain text, each dressed up as a PNG
        for junk in (b"GIF89a" + b"\x00" * 32, b"<svg xmlns='http://www.w3.org/2000/svg'/>", b"hello"):
            s, b = self._raw_post(mgr, f"/api/bikes/{bike}/photo", junk, "image/png")
            self.assertEqual(s, 400, b)
        s, b = mgr.post(f"/api/bikes/{bike}/photo", {"url": "http://x/y.png"})
        self.assertEqual(s, 400, "a JSON body was taken for a photo")
        s, b = self._raw_post(mgr, f"/api/bikes/{bike}/photo",
                              self.PNG + b"\x00" * (5 * 1024 * 1024), "image/png")
        self.assertEqual(s, 413, b)
        self.assertEqual(self._raw_post(self.as_("t.moreno"), f"/api/bikes/{bike}/photo", self.PNG, "image/png")[0], 403)
        self.assertEqual(self._raw_post(self.as_("m.alvarez"), f"/api/bikes/{bike}/photo", self.PNG, "image/png")[0], 403)
        self.assertEqual(self._raw_post(self.anon(), f"/api/bikes/{bike}/photo", self.PNG, "image/png")[0], 401)
        _, page = self.anon().get(f"/api/bikes/{bike}")
        self.assertIsNone(page["photo"], "a refused upload left a photo behind")
        # admin uploads: no notice
        n0 = len(self._unseen_notices(adm))
        s, b = self._raw_post(adm, f"/api/bikes/{bike}/photo", self.JPG, "image/jpeg")
        self.assertEqual(s, 200, b)
        self.assertFalse(b["notified_admin"])
        self.assertEqual(len(self._unseen_notices(adm)), n0)

    def test_g03_photos_path_cannot_walk_out(self):
        s, _, _ = self._raw_get(self.anon(), "/photos/../data.db")
        self.assertIn(s, (403, 404))
        s, _, _ = self._raw_get(self.anon(), "/photos/..%2F..%2Fapp.py")
        self.assertIn(s, (403, 404))

    # -- a spec under more than one heading ------------------------------------

    def _cats(self, client, bike_id):
        _, specs = client.get(f"/api/bikes/{bike_id}/specs")
        return {c["name"]: c for c in specs["categories"]}

    def test_h01_manager_shows_a_spec_under_other_headings_as_a_pointer(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "MULTICAT TEST", 1999, 1999)
        mgr = self.as_("cb919_dave")
        cats = self._cats(mgr, bike)
        spec = next(s for s in cats["Engine"]["specs"] if s["value_type"] == "text")
        mgr.patch(f"/api/specs/{spec['id']}", {"value": "NGK CR8E"})

        s, b = mgr.post(f"/api/bikes/{bike}/categories", {
            "field_key": spec["field_key"],
            "categories": ["Electrical", "Engine", "General"]})   # Engine is home: kept
        self.assertEqual(s, 200, b)
        self.assertEqual(b["home"], "Engine")
        self.assertEqual(b["also_in"], ["General", "Electrical"], "page order, home dropped")

        cats = self._cats(self.anon(), bike)
        home = next(x for x in cats["Engine"]["specs"] if x["field_key"] == spec["field_key"])
        self.assertEqual(home["also_in"], ["General", "Electrical"])
        # the row itself is still only in Engine; the other headings carry an echo
        for name in ("General", "Electrical"):
            self.assertNotIn(spec["field_key"], [x["field_key"] for x in cats[name]["specs"]],
                             f"the spec was duplicated into {name}")
            echo = next(x for x in cats[name]["echoes"] if x["field_key"] == spec["field_key"])
            self.assertEqual(echo["id"], home["id"], "the echo is not the home row")
            self.assertEqual(echo["value"], "NGK CR8E")
        self.assertEqual(cats["Engine"]["echoes"], [])

        # narrowing the list drops the echo
        s, b = mgr.post(f"/api/bikes/{bike}/categories", {
            "field_key": spec["field_key"], "categories": ["Engine", "Electrical"]})
        self.assertEqual(b["also_in"], ["Electrical"])
        cats = self._cats(self.anon(), bike)
        self.assertNotIn(spec["field_key"], [x["field_key"] for x in cats["General"]["echoes"]])
        # and clearing it entirely
        s, b = mgr.post(f"/api/bikes/{bike}/categories", {
            "field_key": spec["field_key"], "categories": ["Engine"]})
        self.assertEqual(b["also_in"], [])
        cats = self._cats(self.anon(), bike)
        self.assertEqual(cats["Electrical"]["echoes"], [])

    def test_h02_an_echo_can_open_a_heading_the_bike_did_not_have(self):
        """A bike with nothing under Suspension still gets that heading when
        a spec is pointed there, so the pointer has somewhere to be."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "MULTICAT EMPTY TEST", 1999, 1999)  # universal fields only
        cats = self._cats(adm, bike)
        self.assertNotIn("Suspension", cats)
        spec = cats["General"]["specs"][0]
        s, b = adm.post(f"/api/bikes/{bike}/categories", {
            "field_key": spec["field_key"], "categories": ["General", "Suspension"]})
        self.assertEqual(s, 200, b)
        cats = self._cats(self.anon(), bike)
        self.assertIn("Suspension", cats)
        self.assertEqual(cats["Suspension"]["specs"], [])
        self.assertEqual([x["field_key"] for x in cats["Suspension"]["echoes"]], [spec["field_key"]])
        # headings stay in page order even with one added at the end
        names = [c["name"] for c in (adm.get(f"/api/bikes/{bike}/specs")[1])["categories"]]
        self.assertEqual(names, sorted(names, key=app.CATEGORY_ORDER.index))

    def test_h03_placement_is_refused_to_strangers_and_nonsense(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "MULTICAT DENY TEST", 1999, 1999)
        spec = self._cats(adm, bike)["Engine"]["specs"][0]
        body = {"field_key": spec["field_key"], "categories": ["Engine", "Electrical"]}
        self.assertEqual(self.as_("t.moreno").post(f"/api/bikes/{bike}/categories", body)[0], 403)
        self.assertEqual(self.as_("m.alvarez").post(f"/api/bikes/{bike}/categories", body)[0], 403)
        self.assertEqual(self.anon().post(f"/api/bikes/{bike}/categories", body)[0], 401)
        mgr = self.as_("cb919_dave")
        s, b = mgr.post(f"/api/bikes/{bike}/categories",
                        {"field_key": spec["field_key"], "categories": ["Bodywork"]})
        self.assertEqual(s, 400, b); self.assertIn("Bodywork", b["error"])
        self.assertEqual(mgr.post(f"/api/bikes/{bike}/categories",
                                  {"field_key": spec["field_key"], "categories": "Electrical"})[0], 400)
        self.assertEqual(mgr.post(f"/api/bikes/{bike}/categories",
                                  {"field_key": "no_such_field", "categories": ["Engine"]})[0], 404)
        s, b = mgr.post(f"/api/bikes/{bike}/categories",
                        {"field_key": spec["field_key"], "categories": []})
        self.assertEqual(s, 200, "unticking every heading (header only) was refused")
        self.assertEqual(b["shown"], [])
        mgr.post(f"/api/bikes/{bike}/categories", {"field_key": spec["field_key"], "categories": ["Engine"]})
        # a paused spec's echo is hidden from riders along with the row
        mgr.post(f"/api/bikes/{bike}/categories", body)
        mgr.post(f"/api/specs/{spec['id']}/pause", {"paused": True})
        cats = self._cats(self.anon(), bike)
        self.assertNotIn(spec["field_key"], [x["field_key"] for x in cats.get("Electrical", {"echoes": []})["echoes"]])
        cats = self._cats(mgr, bike)
        self.assertIn(spec["field_key"], [x["field_key"] for x in cats["Electrical"]["echoes"]])

    def test_h04_a_split_carries_the_placements(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "MULTICAT SPLIT TEST", 1990, 1995)
        spec = self._cats(adm, bike)["Engine"]["specs"][0]
        adm.post(f"/api/bikes/{bike}/categories",
                 {"field_key": spec["field_key"], "categories": ["Engine", "Electrical"]})
        s, r = adm.post(f"/api/bikes/{bike}/split", {"at_year": 1993})
        self.assertEqual(s, 200, r)
        cats = self._cats(adm, r["later"]["bike_id"])
        self.assertIn(spec["field_key"], [x["field_key"] for x in cats["Electrical"]["echoes"]])

    def test_h05_admin_places_a_field_site_wide_and_managers_cannot_undo_it(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "SITECAT TEST", 1999, 1999)
        other = self._new_bike(adm, "SITECAT OTHER", 1999, 1999)
        spec = self._cats(adm, bike)["Engine"]["specs"][0]
        key = spec["field_key"]
        mgr = self.as_("cb919_dave")
        self.assertEqual(mgr.patch(f"/api/admin/fields/{key}/categories",
                                   {"categories": ["Electrical"]})[0], 403)

        s, b = adm.patch(f"/api/admin/fields/{key}/categories",
                         {"categories": ["Electrical", "Engine"]})
        self.assertEqual(s, 200, b)
        self.assertEqual(b["also_in"], ["Electrical"])
        # every bike carrying the field echoes it, including one no manager touched
        for bid in (bike, other):
            cats = self._cats(self.anon(), bid)
            if key not in [x["field_key"] for c in cats.values() for x in c["specs"]]:
                continue
            self.assertIn(key, [x["field_key"] for x in cats["Electrical"]["echoes"]], bid)
            home = next(x for c in cats.values() for x in c["specs"] if x["field_key"] == key)
            self.assertEqual(home["also_in_site"], ["Electrical"])
            self.assertIn("Electrical", home["also_in"])

        # the manager adds a heading of their own; the site-wide one survives
        # them clearing their list
        s, b = mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": ["Engine", "Controls"]})
        self.assertEqual(s, 200, b)
        home = next(x for c in self._cats(mgr, bike).values() for x in c["specs"] if x["field_key"] == key)
        self.assertEqual(home["also_in"], ["Controls", "Electrical"])
        mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": ["Engine"]})
        home = next(x for c in self._cats(mgr, bike).values() for x in c["specs"] if x["field_key"] == key)
        self.assertEqual(home["also_in"], ["Electrical"], "a manager removed a site-wide heading")

        # the admin list shows it; clearing it clears every bike
        _, fl = adm.get("/api/admin/fields?limit=200")
        f = next(x for x in fl["fields"] if x["field_key"] == key)
        self.assertEqual(f["also_in"], ["Electrical"])
        adm.patch(f"/api/admin/fields/{key}/categories", {"categories": ["Engine"]})
        cats = self._cats(self.anon(), bike)
        self.assertNotIn(key, [x["field_key"] for x in cats.get("Electrical", {"echoes": []})["echoes"]])
        self.assertEqual(adm.patch("/api/admin/fields/no_such/categories", {"categories": ["Engine"]})[0], 404)
        s, b = adm.patch(f"/api/admin/fields/{key}/categories", {"categories": []})
        self.assertEqual((s, b["shown"]), (200, []))
        adm.patch(f"/api/admin/fields/{key}/categories", {"categories": ["Engine"]})
        self.assertEqual(adm.patch(f"/api/admin/fields/{key}/categories", {"categories": ["Nope"]})[0], 400)

    # -- email at registration, admin's eyes only --------------------------------

    def test_i01_registration_needs_a_plausible_email(self):
        base = {"username": "mailme", "password": "a-good-long-one"}
        for bad in (None, "", "nope", "a@b", "@example.com", "two@@x.com", "x" * 250 + "@a.bc"):
            body = dict(base, email=bad) if bad is not None else dict(base)
            s, b = Client(self.base).post("/api/auth/register", body)
            self.assertEqual(s, 400, f"{bad!r}: {b}")
        s, b = Client(self.base).post("/api/auth/register", dict(base, email="  MailMe@Example.COM "))
        self.assertEqual(s, 200, b)
        con = sqlite3.connect(self.db)
        stored = con.execute("SELECT email FROM users WHERE username='mailme'").fetchone()[0]
        con.close()
        self.assertEqual(stored, "mailme@example.com", "not trimmed and lower-cased")
        # the same address cannot make a second account, whatever the case
        s, b = Client(self.base).post("/api/auth/register",
                                      dict(base, username="mailme2", email="MAILME@example.com"))
        self.assertEqual(s, 409, b)
        self.assertIn("sign in", b["error"])

    def test_i02_the_email_reaches_admin_and_nobody_else(self):
        c = Client(self.base)
        s, b = c.post("/api/auth/register", {"username": "privatemail", "email": "pm@example.com",
                                              "password": "a-good-long-one"})
        self.assertEqual(s, 200, b)
        self.assertNotIn("email", b["user"], "registration echoed the email back into the page")
        _, me = c.get("/api/auth/me")
        self.assertNotIn("email", me["user"])
        uid = me["user"]["id"]
        _, prof = self.anon().get(f"/api/users/{uid}/profile")
        self.assertNotIn("pm@example.com", json.dumps(prof), "the public profile leaks the email")
        self.assertEqual(self.as_("m.alvarez").get("/api/users")[0], 403)
        self.assertEqual(c.get("/api/users")[0], 403)
        _, lst = self.as_("admin").get("/api/users")
        row = next(u for u in lst["users"] if u["username"] == "privatemail")
        self.assertEqual(row["email"], "pm@example.com")
        # accounts from before the column simply have none
        old = next(u for u in lst["users"] if u["username"] == "m.alvarez")
        self.assertIsNone(old["email"])

    def test_h06_a_spec_can_leave_its_home_heading(self):
        """The home heading is the usual place, not a fixture. Untick it and
        the spec leads under the first heading it still shows in."""
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "HOME OPTIONAL TEST", 1999, 1999)
        spec = next(s for s in self._cats(adm, bike)["Engine"]["specs"] if s["value_type"] == "text")
        key = spec["field_key"]
        mgr = self.as_("cb919_dave")
        s, b = mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": ["Electrical"]})
        self.assertEqual(s, 200, b)
        self.assertFalse(b["home_shown"]); self.assertEqual(b["shown"], ["Electrical"])
        cats = self._cats(self.anon(), bike)
        self.assertNotIn(key, [x["field_key"] for x in cats["Engine"]["specs"]], "still under its home")
        row = next(x for x in cats["Electrical"]["specs"] if x["field_key"] == key)
        self.assertEqual((row["lead"], row["also_in"], row["home_shown"], row["category"]),
                         ("Electrical", [], False, "Engine"))
        self.assertEqual(cats["Electrical"]["echoes"], [])
        # two headings, neither the home: it leads under the first in page order
        mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": ["Electrical", "Drive"]})
        cats = self._cats(self.anon(), bike)
        row = next(x for x in cats["Drive"]["specs"] if x["field_key"] == key)
        self.assertEqual((row["lead"], row["also_in"]), ("Drive", ["Electrical"]))
        self.assertIn(key, [x["field_key"] for x in cats["Electrical"]["echoes"]])
        # and back home
        mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": ["Engine"]})
        cats = self._cats(self.anon(), bike)
        self.assertIn(key, [x["field_key"] for x in cats["Engine"]["specs"]])

    def test_h07_admin_can_take_a_field_out_of_its_home_everywhere(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "HOME SITE TEST", 1999, 1999)
        spec = self._cats(adm, bike)["Engine"]["specs"][0]
        key = spec["field_key"]
        s, b = adm.patch(f"/api/admin/fields/{key}/categories", {"categories": ["Electrical"]})
        self.assertEqual(s, 200, b)
        self.assertEqual((b["home_shown"], b["shown"]), (False, ["Electrical"]))
        cats = self._cats(self.anon(), bike)
        self.assertNotIn(key, [x["field_key"] for c in cats.values() if c["name"] == "Engine" for x in c["specs"]])
        row = next(x for x in cats["Electrical"]["specs"] if x["field_key"] == key)
        self.assertTrue(row["home_locked"], "the manager should not be able to put it back")
        # the manager cannot bring it home while admin has it out
        mgr = self.as_("cb919_dave")
        s, b = mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": ["Engine"]})
        self.assertEqual(s, 200, b)
        self.assertFalse(b["home_shown"])
        # admin list says so
        _, fl = adm.get("/api/admin/fields?limit=200")
        f = next(x for x in fl["fields"] if x["field_key"] == key)
        self.assertFalse(f["home_shown"])
        adm.patch(f"/api/admin/fields/{key}/categories", {"categories": ["Engine"]})
        cats = self._cats(self.anon(), bike)
        self.assertIn(key, [x["field_key"] for x in cats["Engine"]["specs"]])

    def test_h08_no_heading_means_header_only(self):
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "NO HEADING TEST", 1999, 1999)
        eng = next(s for s in self._cats(adm, bike)["Engine"]["specs"] if s["value_type"] == "text")
        gen = self._cats(adm, bike)["General"]["specs"][0]
        mgr = self.as_("cb919_dave")
        adm.post(f"/api/bikes/{bike}/header", {"field_key": eng["field_key"]})   # pinned
        for key in (eng["field_key"], gen["field_key"]):
            s, b = mgr.post(f"/api/bikes/{bike}/categories", {"field_key": key, "categories": []})
            self.assertEqual(s, 200, b)
            self.assertEqual((b["shown"], b["also_in"]), ([], []))
        _, specs = self.anon().get(f"/api/bikes/{bike}/specs")
        under = [x["field_key"] for c in specs["categories"] for x in c["specs"] + c["echoes"]]
        self.assertNotIn(eng["field_key"], under); self.assertNotIn(gen["field_key"], under)
        un = {x["field_key"]: x for x in specs["unplaced"]}
        self.assertIn(eng["field_key"], un); self.assertIn(gen["field_key"], un)
        self.assertTrue(un[eng["field_key"]]["in_header"], "the pin must survive, it is the only place left")
        self.assertIsNone(un[eng["field_key"]]["lead"])
        self.assertEqual(un[gen["field_key"]]["category"], "General")
        # the manager can still act on it -- it is the same row
        s, _ = mgr.patch(f"/api/specs/{eng['id']}", {"value": "still editable"})
        self.assertEqual(s, 200)
        # an offline unplaced spec is hidden from riders like any other
        mgr.post(f"/api/specs/{eng['id']}/pause", {"paused": True})
        _, specs = self.anon().get(f"/api/bikes/{bike}/specs")
        self.assertNotIn(eng["field_key"], [x["field_key"] for x in specs["unplaced"]])
        _, specs = mgr.get(f"/api/bikes/{bike}/specs")
        self.assertIn(eng["field_key"], [x["field_key"] for x in specs["unplaced"]])

    # -- asking for a spec the bike does not list -------------------------
    def test_j01_a_rider_can_ask_for_a_field_the_bike_lacks(self):
        """A fresh bike carries only the universal fields, so nearly the whole
        tree is missing from it. Two riders ask for the same one; the count is
        the people who asked, and the same rider cannot ask twice."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FIELDREQ TEST", 2001, 2002)
        s, b = self.anon().get(f"/api/bikes/{bike}/field-requests")
        self.assertEqual(s, 200)
        st = {f["field_key"]: f["status"] for f in b["fields"]}
        self.assertEqual(st["coolant_capacity"], "missing")
        self.assertEqual(st["fuel_octane_grade"], "online", "a field the bike has shows as online here")

        rider = self.as_("sohc_sam")
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_key": "coolant_capacity", "reasoning": "flushing the rad"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["kind"], r["requests"]), ("request", 1))
        s, r = rider.post(f"/api/bikes/{bike}/field-requests", {"field_key": "coolant_capacity"})
        self.assertEqual(s, 409)
        s, r = self.as_("two_stroke_tina").post(f"/api/bikes/{bike}/field-requests",
                                                {"field_key": "coolant_capacity"})
        self.assertEqual(r["requests"], 2)

        s, b = rider.get(f"/api/bikes/{bike}/field-requests")
        cc = next(f for f in b["fields"] if f["field_key"] == "coolant_capacity")
        self.assertEqual((cc["requests"], cc["mine"]), (2, 1))
        # asking for a field the bike already has is refused: that is "request this spec"
        s, r = rider.post(f"/api/bikes/{bike}/field-requests", {"field_key": "fuel_octane_grade"})
        self.assertEqual(s, 409)
        # and anonymous cannot ask at all
        self.assertEqual(self.anon().post(f"/api/bikes/{bike}/field-requests",
                                          {"field_key": "battery"})[0], 401)

    def test_j02_the_manager_adds_it_and_every_request_closes(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FIELDREQ MGR", 2003, 2004)
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": self._uid("gp_hayes")})
        for who in ("sohc_sam", "two_stroke_tina"):
            self.as_(who).post(f"/api/bikes/{bike}/field-requests", {"field_key": "battery"})

        # not the manager of this bike -> refused
        s, _ = self.as_("m.alvarez").post(f"/api/bikes/{bike}/field-requests/battery/decide",
                                          {"status": "added"})
        self.assertEqual(s, 403)

        mgr = self.as_("gp_hayes")
        s, q = mgr.get("/api/manager/field-requests")
        self.assertEqual(s, 200)
        item = next(i for i in q["items"] if i["bike_id"] == bike)
        self.assertEqual((item["field_key"], item["requests"]), ("battery", 2))
        self.assertEqual({a["username"] for a in item["asked_by"]}, {"sohc_sam", "two_stroke_tina"})

        s, r = mgr.post(f"/api/bikes/{bike}/field-requests/battery/decide", {"status": "added"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["added"], r["resolved"]), (True, 2))
        # the field is on the bike now, empty and pending; the queue is clear
        con = sqlite3.connect(self.db)
        row = con.execute("SELECT value, confidence FROM specs WHERE bike_id=? AND field_key='battery'",
                          (bike,)).fetchone()
        left = con.execute("SELECT COUNT(*) FROM field_requests WHERE bike_id=? AND status='pending'",
                           (bike,)).fetchone()[0]
        con.close()
        self.assertEqual(tuple(row), (None, "pending"))
        self.assertEqual(left, 0)
        st = {f["field_key"]: f["status"] for f in
              self.anon().get(f"/api/bikes/{bike}/field-requests")[1]["fields"]}
        self.assertEqual(st["battery"], "online")

    def test_j03_declining_needs_no_field_and_admin_can_settle_an_unmanaged_bike(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FIELDREQ ADMIN", 2005, 2005)
        self.as_("sohc_sam").post(f"/api/bikes/{bike}/field-requests", {"field_key": "drive_belt"})
        s, q = adm.get("/api/admin/field-requests")
        item = next(i for i in q["items"] if i["bike_id"] == bike)
        self.assertFalse(item["has_manager"])
        s, r = adm.post(f"/api/bikes/{bike}/field-requests/drive_belt/decide",
                        {"status": "declined", "note": "chain drive"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["added"], r["resolved"]), (False, 1))
        con = sqlite3.connect(self.db)
        self.assertIsNone(con.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key='drive_belt'",
                                      (bike,)).fetchone())
        self.assertEqual(con.execute("SELECT status, admin_note FROM field_requests WHERE bike_id=?",
                                     (bike,)).fetchone(), ("declined", "chain drive"))
        con.close()

    def test_j04_a_spec_not_on_the_tree_becomes_a_proposal_for_admin(self):
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FIELDREQ NEW", 2006, 2006)
        rider = self.as_("sohc_sam")
        # something the tree already has, typed by name -> pointed at the list instead
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_name": "Coolant Capacity", "category": "Engine"})
        self.assertEqual(s, 409)
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_name": "Steering Head Bearing Part Number", "category": "Suspension",
                           "reasoning": "replaced mine, no number anywhere"})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["kind"], "proposal")
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_name": "steering head bearing part number", "category": "Suspension"})
        self.assertEqual(s, 409, "the same rider proposing the same thing twice")
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_name": "Something", "category": "Nowhere"})
        self.assertEqual(s, 400)
        # admin sees it in the proposals queue, filed under this bike by the rider
        s, q = adm.get("/api/admin/proposals")
        p = next(p for p in q["items"] if p["field_name"] == "Steering Head Bearing Part Number")
        self.assertEqual(p["bike_id"], bike)
        # and the bike's request page lists it as open
        s, b = rider.get(f"/api/bikes/{bike}/field-requests")
        self.assertEqual([x["field_name"] for x in b["proposals"]], ["Steering Head Bearing Part Number"])
        self.assertTrue(b["proposals"][0]["mine"])

    # -- what a field's value IS: text, or a closed vocabulary ---------------
    def test_k01_a_new_field_is_text_unless_a_type_is_chosen(self):
        adm = self.as_("admin")
        s, r = adm.post("/api/admin/fields", {"label": "Neutral Switch Wire Color",
                                              "category": "Electrical",
                                              "bike_ids": [self.cb919]})
        self.assertEqual(s, 200, r)
        con = sqlite3.connect(self.db)
        vt = con.execute("SELECT value_type FROM spec_fields WHERE field_key='neutral_switch_wire_color'").fetchone()[0]
        con.close()
        self.assertEqual(vt, "text", "nothing chosen -> a text box, whatever the name says")
        s, r = adm.post("/api/admin/fields", {"label": "Clutch Switch Wire Color",
                                              "category": "Electrical", "value_type": "wire_color",
                                              "bike_ids": [self.cb919]})
        self.assertEqual(s, 200, r)
        s, r = adm.post("/api/admin/fields", {"label": "Bad Type", "category": "Electrical",
                                              "value_type": "colour", "bike_ids": [self.cb919]})
        self.assertEqual(s, 400)
        # the wire field refuses free text and takes a colour, the text field takes anything
        con = sqlite3.connect(self.db)
        wire_spec, text_spec = [con.execute(
            "SELECT id FROM specs WHERE bike_id=? AND field_key=?", (self.cb919, k)).fetchone()[0]
            for k in ("clutch_switch_wire_color", "neutral_switch_wire_color")]
        con.close()
        mgr = self.as_("m.alvarez")
        self.assertEqual(mgr.patch(f"/api/specs/{wire_spec}", {"value": "Lg/Bk"})[0], 400)
        self.assertEqual(mgr.patch(f"/api/specs/{wire_spec}", {"value": "light green/black"})[0], 200)
        self.assertEqual(mgr.patch(f"/api/specs/{text_spec}", {"value": "Lg/Bk"})[0], 200)

    def test_k02_changing_a_field_to_wire_colour_checks_every_value_first(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        s, r = adm.post("/api/admin/fields", {"label": "Horn Wire Color", "category": "Electrical",
                                              "bike_ids": [self.cb919]})
        self.assertEqual(s, 200, r)
        con = sqlite3.connect(self.db)
        spec = con.execute("SELECT id FROM specs WHERE bike_id=? AND field_key='horn_wire_color'",
                           (self.cb919,)).fetchone()[0]
        con.close()
        # a loosely typed but readable colour, and one that is not a colour at all
        self.assertEqual(mgr.patch(f"/api/specs/{spec}", {"value": "Yellow / Red"})[0], 200)
        s, r = adm.patch("/api/admin/fields/horn_wire_color/value-type", {"value_type": "wire_color"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["was"], r["value_type"], r["rewritten"]), ("text", "wire_color", 1))
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT value FROM specs WHERE id=?", (spec,)).fetchone()[0],
                         "yellow/red", "the readable value took its canonical form")
        con.close()
        # back to text is always allowed; then a non-colour goes in ...
        self.assertEqual(adm.patch("/api/admin/fields/horn_wire_color/value-type",
                                   {"value_type": "text"})[0], 200)
        self.assertEqual(mgr.patch(f"/api/specs/{spec}", {"value": "see diagram p.12"})[0], 200)
        # ... and now the switch is refused, naming the value in the way
        s, r = adm.patch("/api/admin/fields/horn_wire_color/value-type", {"value_type": "wire_color"})
        self.assertEqual(s, 409)
        self.assertIn("see diagram p.12", r["error"])
        # a manager cannot change value types at all
        self.assertEqual(mgr.patch("/api/admin/fields/horn_wire_color/value-type",
                                   {"value_type": "text"})[0], 403)

    def test_k03_an_approved_proposal_takes_the_chosen_type(self):
        adm = self.as_("admin")
        mgr = self.as_("m.alvarez")
        s, r = mgr.post("/api/proposals", {"field_name": "Sidestand Switch Wire Color",
                                           "category": "Electrical", "reasoning": "two wires"})
        self.assertEqual(s, 200, r)
        s, d = adm.post(f"/api/admin/proposals/{r['id']}/decide",
                        {"status": "approved", "value_type": "wire_color"})
        self.assertEqual(s, 200, d)
        con = sqlite3.connect(self.db)
        vt = con.execute("SELECT value_type FROM spec_fields WHERE field_key='sidestand_switch_wire_color'").fetchone()[0]
        con.close()
        self.assertEqual(vt, "wire_color")

    def test_k04_a_proposal_carries_the_value_type_it_asked_for(self):
        """A rider or manager says what the values will be; it defaults to
        text, and admin's approval starts from what was asked."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "VT PROPOSAL", 2008, 2008)
        rider = self.as_("sohc_sam")
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_name": "Fan Switch Wire Color", "category": "Electrical",
                           "value_type": "wire_color"})
        self.assertEqual(s, 200, r)
        s, r2 = rider.post(f"/api/bikes/{bike}/field-requests",
                           {"field_name": "Rear Rack Part Number", "category": "General"})
        self.assertEqual(s, 200, r2)
        s, q = adm.get("/api/admin/proposals")
        by = {p["field_name"]: p for p in q["items"]}
        self.assertEqual(by["Fan Switch Wire Color"]["value_type"], "wire_color")
        self.assertEqual(by["Rear Rack Part Number"]["value_type"], "text", "nothing chosen -> text")
        # approving without a choice takes the proposer's; naming one overrides it
        s, d = adm.post(f"/api/admin/proposals/{r['id']}/decide", {"status": "approved"})
        self.assertEqual(s, 200, d)
        s, d2 = adm.post(f"/api/admin/proposals/{r2['id']}/decide",
                         {"status": "approved", "value_type": "fuel_octane"})
        self.assertEqual(s, 200, d2)
        con = sqlite3.connect(self.db)
        got = dict(con.execute("SELECT field_key, value_type FROM spec_fields"
                               " WHERE field_key IN ('fan_switch_wire_color', 'rear_rack_part_number')").fetchall())
        con.close()
        self.assertEqual(got, {"fan_switch_wire_color": "wire_color", "rear_rack_part_number": "fuel_octane"})

    def test_k05_the_request_page_says_where_a_field_stands_on_this_bike(self):
        """Online here, offline here, or not on this bike -- about THIS bike,
        not the tree at large. An offline one can be asked for, and the
        manager's "add" puts it online."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "VT STATUS", 2009, 2009)
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": self._uid("gp_hayes")})
        s, r = adm.post("/api/admin/fields", {"label": "Staged Thing", "category": "General",
                                              "bike_ids": [bike], "offline": True})
        self.assertEqual(s, 200, r)
        s, b = self.anon().get(f"/api/bikes/{bike}/field-requests")
        st = {f["field_key"]: f["status"] for f in b["fields"]}
        self.assertEqual(st["fuel_octane_grade"], "online")     # universal, on the bike
        self.assertEqual(st["staged_thing"], "offline")         # on the bike, staged
        self.assertEqual(st["battery"], "missing")              # never answered the questionnaire
        rider = self.as_("sohc_sam")
        self.assertEqual(rider.post(f"/api/bikes/{bike}/field-requests",
                                    {"field_key": "fuel_octane_grade"})[0], 409)
        s, r = rider.post(f"/api/bikes/{bike}/field-requests", {"field_key": "staged_thing"})
        self.assertEqual(s, 200, r)
        s, r = self.as_("gp_hayes").post(f"/api/bikes/{bike}/field-requests/staged_thing/decide",
                                         {"status": "added"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["added"], r["resolved"]), (True, 1))
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT paused FROM specs WHERE bike_id=? AND field_key='staged_thing'",
                                     (bike,)).fetchone()[0], 0)
        con.close()

    def test_l01_gear_and_accessories_is_last_and_on_every_bike(self):
        """Helmet, gloves, boots, goggles, heated vest, jacket: community
        fields (riders' choices, no factory value), on every bike, under a
        heading that comes after Electrical."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "GEAR TEST", 2010, 2010)
        s, b = self.anon().get(f"/api/bikes/{bike}/specs")
        self.assertEqual(s, 200)
        names = [c["name"] for c in b["categories"]]
        self.assertEqual(names[-1], "Gear and Accessories")
        gear = b["categories"][-1]["specs"]
        self.assertEqual([g["label"] for g in gear],
                         ["Helmet", "Gloves", "Boots", "Goggles", "Heated Vest", "Jacket"])
        self.assertTrue(all(g["spec_type"] == "community" for g in gear))
        # a rider can suggest one straight away: community fields take alternates
        spec = gear[0]["id"]
        s, r = self.as_("sohc_sam").post(f"/api/specs/{spec}/alternates", {"text": "Shoei RF-1400"})
        self.assertEqual(s, 200, r)

    def test_m01_an_empty_spec_can_be_flagged_and_removed(self):
        """A field that does not belong on a bike has no value to flag -- the
        flag must still work, and the manager must be able to take the field
        off the bike. Only while it is empty: a value is somebody's work."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FLAG EMPTY", 2011, 2011)
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": self._uid("gp_hayes")})
        s, r = adm.post("/api/admin/fields/coolant_capacity/apply", {"bike_ids": [bike]})
        self.assertEqual(s, 200, r)
        con = sqlite3.connect(self.db)
        spec = con.execute("SELECT id FROM specs WHERE bike_id=? AND field_key='coolant_capacity'",
                           (bike,)).fetchone()[0]
        con.close()
        rider = self.as_("sohc_sam")
        s, r = rider.post(f"/api/specs/{spec}/flags", {"reason": "irrelevant", "detail": "air-cooled"})
        self.assertEqual(s, 200, r)
        mgr = self.as_("gp_hayes")
        s, q = mgr.get("/api/manager/flags")
        f = next(x for x in q["flags"] if x["spec_id"] == spec)
        self.assertEqual((f["current_value"], f["field_key"]), (None, "coolant_capacity"))
        # a rider cannot remove it, the manager of another bike cannot, this one can
        self.assertEqual(rider.delete(f"/api/bikes/{bike}/specs/coolant_capacity")[0], 403)
        self.assertEqual(self.as_("m.alvarez").delete(f"/api/bikes/{bike}/specs/coolant_capacity")[0], 403)
        s, r = mgr.delete(f"/api/bikes/{bike}/specs/coolant_capacity")
        self.assertEqual(s, 200, r)
        self.assertEqual(r["flags_closed"], 1)
        con = sqlite3.connect(self.db)
        self.assertIsNone(con.execute("SELECT 1 FROM specs WHERE id=?", (spec,)).fetchone())
        con.close()
        # but not once it holds a value
        s, r = adm.post("/api/admin/fields/coolant_capacity/apply", {"bike_ids": [bike]})
        con = sqlite3.connect(self.db)
        spec2 = con.execute("SELECT id FROM specs WHERE bike_id=? AND field_key='coolant_capacity'",
                            (bike,)).fetchone()[0]
        con.close()
        self.assertEqual(mgr.patch(f"/api/specs/{spec2}", {"value": "1.2 L"})[0], 200)
        self.assertEqual(mgr.delete(f"/api/bikes/{bike}/specs/coolant_capacity")[0], 409)

    def test_n01_a_field_can_start_offline_on_a_kind_of_bike(self):
        """Sprockets on a scooter: the belt branch puts them there for the
        bikes with pulleys, so on a Scooter they are created offline. The
        manager can still put one online; rows already showing are untouched;
        admin edits the rule from the tree."""
        adm = self.as_("admin")
        scooter = self._new_bike(adm, "OFFLINE DEFAULT SCOOTER", 2012, 2012)
        cruiser = self._new_bike(adm, "OFFLINE DEFAULT CRUISER", 2012, 2012)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE bikes SET bike_type='Scooter' WHERE id=?", (scooter,))
        con.execute("UPDATE bikes SET bike_type='Cruiser' WHERE id=?", (cruiser,))
        con.commit(); con.close()
        s, r = adm.post("/api/admin/fields/front_sprocket/apply", {"bike_ids": [scooter, cruiser]})
        self.assertEqual(s, 200, r)
        con = sqlite3.connect(self.db)
        paused = dict(con.execute("SELECT bike_id, paused FROM specs WHERE field_key='front_sprocket'"
                                  " AND bike_id IN (?,?)", (scooter, cruiser)).fetchall())
        con.close()
        self.assertEqual((paused[scooter], paused[cruiser]), (1, 0))
        # the public sheet of the scooter does not show it; admin (manages any bike) can put it online
        s, b = self.anon().get(f"/api/bikes/{scooter}/specs")
        self.assertNotIn("Front Sprocket", [x["label"] for c in b["categories"] for x in c["specs"]])
        con = sqlite3.connect(self.db)
        spec = con.execute("SELECT id FROM specs WHERE bike_id=? AND field_key='front_sprocket'", (scooter,)).fetchone()[0]
        con.close()
        s, r = adm.post(f"/api/specs/{spec}/pause", {"paused": False})
        self.assertEqual(s, 200, r)
        s, b = self.anon().get(f"/api/bikes/{scooter}/specs")
        self.assertIn("Front Sprocket", [x["label"] for c in b["categories"] for x in c["specs"]])
        # the rule is admin's to edit, and the tree listing shows it
        s, b = adm.get("/api/admin/fields?q=front%20sprocket")
        f = next(x for x in b["fields"] if x["field_key"] == "front_sprocket")
        self.assertEqual(f["offline_on"], ["Scooter"])
        s, r = adm.patch("/api/admin/fields/front_sprocket/offline-defaults", {"bike_types": ["Scooter", "Cruiser"]})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["bike_types"], ["Cruiser", "Scooter"])
        self.assertEqual(adm.patch("/api/admin/fields/front_sprocket/offline-defaults",
                                   {"bike_types": ["Hovercraft"]})[0], 400)
        self.assertEqual(self.as_("m.alvarez").patch("/api/admin/fields/front_sprocket/offline-defaults",
                                                     {"bike_types": []})[0], 403)
        adm.patch("/api/admin/fields/front_sprocket/offline-defaults", {"bike_types": ["Scooter"]})

    def test_o01_a_flag_can_be_taken_back_by_mistake(self):
        """The page gets the flag's id back as my_flag, so "flagged by
        mistake" can withdraw exactly that one -- yours only, and only while
        it is still open."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "WITHDRAW FLAG", 2013, 2013)
        rider = self.as_("sohc_sam")
        spec = self._empty_spec(rider, bike)
        s, r = rider.post(f"/api/specs/{spec['id']}/flags", {"reason": "irrelevant"})
        self.assertEqual(s, 200, r)
        s, b = rider.get(f"/api/bikes/{bike}/specs")
        mine = next(x for c in b["categories"] for x in c["specs"] if x["id"] == spec["id"])
        self.assertEqual(mine["my_flag"], r["id"])
        # somebody else sees the count but no flag of their own
        s, b = self.as_("two_stroke_tina").get(f"/api/bikes/{bike}/specs")
        theirs = next(x for c in b["categories"] for x in c["specs"] if x["id"] == spec["id"])
        self.assertEqual((theirs["my_flag"], theirs["open_flags"]), (None, 1))
        self.assertEqual(self.as_("two_stroke_tina").delete(f"/api/flags/{r['id']}")[0], 403)
        s, _ = rider.delete(f"/api/flags/{r['id']}")
        self.assertEqual(s, 200)
        s, b = rider.get(f"/api/bikes/{bike}/specs")
        gone = next(x for c in b["categories"] for x in c["specs"] if x["id"] == spec["id"])
        self.assertEqual((gone["my_flag"], gone["open_flags"]), (None, 0))


    def test_p01_a_manager_works_the_tree_for_their_own_bike(self):
        """The Spec Tree, read against one bike, says where every field stands
        there -- and the bike's manager gets admin's controls scoped to that
        machine: add a field, take it offline or online, take it off (while
        empty), override its type, choose its headings. Never another bike."""
        adm = self.as_("admin")
        mine = self._new_bike(adm, "TREE MINE", 2014, 2014)
        other = self._new_bike(adm, "TREE OTHER", 2014, 2014)
        adm.post(f"/api/admin/bikes/{mine}/manager", {"user_id": self._uid("gp_hayes")})
        mgr = self.as_("gp_hayes")

        # the plain tree lists the manager's bikes; against a bike, each field says where it stands
        s, t = mgr.get("/api/spec-tree")
        self.assertEqual(s, 200, t)
        self.assertIn(mine, [b["bike_id"] for b in t["my_bikes"]])
        self.assertIsNone(t["bike"])
        self.assertNotIn("here", t["fields"][0])
        self.assertEqual(mgr.get(f"/api/spec-tree?bike={other}")[0], 403)
        self.assertEqual(mgr.get("/api/spec-tree?bike=abc")[0], 400)
        self.assertEqual(mgr.get("/api/spec-tree?bike=999999")[0], 404)
        s, t = mgr.get(f"/api/spec-tree?bike={mine}")
        self.assertEqual(s, 200, t)
        self.assertEqual(t["bike"]["bike_id"], mine)
        cc = next(f for f in t["fields"] if f["field_key"] == "coolant_capacity")
        self.assertIsNone(cc["here"])

        # add it: an empty pending row on this bike only, riders' requests answered
        rider = self.as_("sohc_sam")
        s, r = rider.post(f"/api/bikes/{mine}/field-requests",
                          {"field_key": "coolant_capacity", "reasoning": "it is liquid cooled"})
        self.assertEqual(s, 200, r)
        self.assertEqual(rider.post(f"/api/bikes/{mine}/specs/coolant_capacity", {})[0], 403)
        self.assertEqual(self.as_("m.alvarez").post(f"/api/bikes/{mine}/specs/coolant_capacity", {})[0], 403)
        self.assertEqual(mgr.post(f"/api/bikes/{mine}/specs/no_such_field", {})[0], 404)
        s, r = mgr.post(f"/api/bikes/{mine}/specs/coolant_capacity", {})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["paused"], r["resolved"], r["label"]), (False, 1, "Coolant Capacity"))
        self.assertEqual(mgr.post(f"/api/bikes/{mine}/specs/coolant_capacity", {})[0], 409)
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT value, confidence FROM specs WHERE id=?",
                                     (r["spec_id"],)).fetchone(), (None, "pending"))
        self.assertIsNone(con.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key='coolant_capacity'",
                                      (other,)).fetchone())
        self.assertEqual(con.execute("SELECT status FROM field_requests WHERE bike_id=? AND field_key='coolant_capacity'",
                                     (mine,)).fetchone()[0], "added")
        con.close()
        s, t = mgr.get(f"/api/spec-tree?bike={mine}")
        cc = next(f for f in t["fields"] if f["field_key"] == "coolant_capacity")
        self.assertEqual((cc["here"]["rows"], cc["here"]["online"], cc["here"]["values"], cc["here"]["spec_ids"]),
                         (1, 1, 0, [r["spec_id"]]))

        # offline, a type override and an extra heading all read back per bike
        spec_id = r["spec_id"]
        self.assertEqual(mgr.post(f"/api/specs/{spec_id}/pause", {"paused": True})[0], 200)
        self.assertEqual(mgr.patch(f"/api/specs/{spec_id}/type", {"spec_type": "fixed"})[0], 200)
        s, r2 = mgr.post(f"/api/bikes/{mine}/categories",
                         {"field_key": "coolant_capacity", "categories": ["Engine", "General"]})
        self.assertEqual(s, 200, r2)
        s, t = mgr.get(f"/api/spec-tree?bike={mine}")
        cc = next(f for f in t["fields"] if f["field_key"] == "coolant_capacity")
        self.assertEqual(cc["here"]["online"], 0)
        self.assertEqual(cc["here"]["spec_type"], "fixed")
        self.assertIn("General", cc["here"]["also_in"])
        self.assertFalse(cc["here"]["home_hidden"])
        self.assertEqual(cc["also_in_site"], [])

        # and off again, while it is empty -- its per-bike heading goes with it,
        # so adding it back later starts clean
        s, r3 = mgr.delete(f"/api/bikes/{mine}/specs/coolant_capacity")
        self.assertEqual(s, 200, r3)
        s, t = mgr.get(f"/api/spec-tree?bike={mine}")
        cc = next(f for f in t["fields"] if f["field_key"] == "coolant_capacity")
        self.assertIsNone(cc["here"])
        con = sqlite3.connect(self.db)
        self.assertIsNone(con.execute(
            "SELECT 1 FROM bike_spec_categories WHERE bike_id=? AND field_key='coolant_capacity'",
            (mine,)).fetchone())
        con.close()

    def test_p02_adding_from_the_tree_honours_the_offline_default(self):
        """A sprocket added to a scooter from the tree starts offline, and the
        reply says so, so the page can tell the manager rather than leave
        them wondering why riders cannot see it."""
        adm = self.as_("admin")
        scooter = self._new_bike(adm, "TREE SCOOTER", 2015, 2015)
        con = sqlite3.connect(self.db)
        con.execute("UPDATE bikes SET bike_type='Scooter' WHERE id=?", (scooter,))
        con.execute("DELETE FROM specs WHERE bike_id=? AND field_key='front_sprocket'", (scooter,))
        con.commit(); con.close()
        s, r = adm.post(f"/api/bikes/{scooter}/specs/front_sprocket", {})
        self.assertEqual(s, 200, r)
        self.assertTrue(r["paused"])
        s, t = adm.get(f"/api/spec-tree?bike={scooter}")
        self.assertEqual(s, 200, t)
        self.assertEqual(t["my_bikes"], [])   # admin searches the catalog instead
        fs = next(f for f in t["fields"] if f["field_key"] == "front_sprocket")
        self.assertEqual((fs["here"]["rows"], fs["here"]["online"]), (1, 0))


    def test_q01_a_rider_can_ask_for_a_bike_and_admin_creates_it(self):
        """A bike the catalogue lacks: a rider asks, others add their name
        rather than filing again, and admin creates it from the request --
        with the model code and years corrected -- answering everyone."""
        rider = self.as_("sohc_sam")
        # anyone can read the list; only a signed-in rider can ask
        s, b = self.anon().get("/api/bike-requests")
        self.assertEqual(s, 200, b)
        self.assertIn("Honda", b["makes"])
        self.assertIn("Scooter", b["bike_types"])
        self.assertEqual(self.anon().post("/api/bike-requests", {"make": "Kawasaki", "model": "KLR650"})[0], 401)
        self.assertEqual(rider.post("/api/bike-requests", {"make": "Kawasaki"})[0], 400)
        self.assertEqual(rider.post("/api/bike-requests", {"make": "Kawasaki", "model": "KLR650",
                                                           "year_from": "1700"})[0], 400)
        self.assertEqual(rider.post("/api/bike-requests", {"make": "Kawasaki", "model": "KLR650",
                                                           "year_from": 2010, "year_to": 2008})[0], 400)
        self.assertEqual(rider.post("/api/bike-requests", {"make": "Kawasaki", "model": "KLR650",
                                                           "bike_type": "Hovercraft"})[0], 400)
        self.assertEqual(rider.post("/api/bike-requests", {"make": "Kawasaki", "model": "KLR650",
                                                           "source_url": "ftp://x"})[0], 400)
        # a bike that is already here is pointed out, not requested
        s, r = rider.post("/api/bike-requests", {"make": "honda", "model": "cb919"})
        self.assertEqual(s, 409, r)
        self.assertIn("already in the catalogue", r["error"])

        s, r = rider.post("/api/bike-requests", {
            "make": "Kawasaki", "model": "KLR650", "year_from": 2008, "year_to": 2018,
            "displacement": "651cc single", "bike_type": "Dual-sport / Adventure",
            "reasoning": "I own one and the specs are scattered", "source_url": "https://example.org/klr"})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["kind"], r["riders"]), ("new", 1))
        rid = r["request_id"]
        # the same bike again, from another rider, joins rather than duplicates
        tina = self.as_("two_stroke_tina")
        s, r2 = tina.post("/api/bike-requests", {"make": "kawasaki", "model": "KLR 650", "year_from": 2012})
        self.assertEqual(s, 200, r2)
        self.assertEqual((r2["kind"], r2["request_id"], r2["riders"]), ("joined", rid, 2))
        # ...unless the years cannot be the same machine
        s, r3 = tina.post("/api/bike-requests", {"make": "Kawasaki", "model": "KLR650", "year_from": 2022})
        self.assertEqual(s, 200, r3)
        self.assertEqual(r3["kind"], "new")
        s, b = tina.get("/api/bike-requests")
        mine = next(x for x in b["pending"] if x["id"] == rid)
        self.assertEqual((mine["riders"], mine["mine"], mine["asked_by"]), (2, True, "sohc_sam"))
        # take a name off, put it back
        s, r4 = tina.post(f"/api/bike-requests/{rid}/support", {"on": False})
        self.assertEqual((s, r4["riders"], r4["withdrawn"]), (200, 1, False))
        s, r4 = tina.post(f"/api/bike-requests/{rid}/support", {})
        self.assertEqual((s, r4["on"], r4["riders"]), (200, True, 2))
        # a request nobody waits on any more is gone
        s, r5 = tina.post(f"/api/bike-requests/{r3['request_id']}/support", {"on": False})
        self.assertEqual((s, r5["withdrawn"]), (200, True))

        # admin sees the queue with a hint at bikes already here, then creates it
        adm = self.as_("admin")
        self.assertEqual(rider.get("/api/admin/bike-requests")[0], 403)
        s, q = adm.get("/api/admin/bike-requests")
        self.assertEqual(s, 200, q)
        it = next(x for x in q["items"] if x["id"] == rid)
        self.assertEqual([u["username"] for u in it["supporters"]], ["sohc_sam", "two_stroke_tina"])
        self.assertEqual(rider.post(f"/api/admin/bike-requests/{rid}/decide", {"status": "added"})[0], 403)
        self.assertEqual(adm.post(f"/api/admin/bike-requests/{rid}/decide", {"status": "maybe"})[0], 400)
        s, d = adm.post(f"/api/admin/bike-requests/{rid}/decide", {
            "status": "added", "model_code": "KL650E", "name": "Kawasaki KLR650",
            "year_start": 2008, "year_end": 2018, "note": "Added as the 2008-2018 generation"})
        self.assertEqual(s, 200, d)
        self.assertTrue(d["created"])
        self.assertEqual(d["riders"], 2)
        s, bike = self.anon().get(f"/api/bikes/{d['bike_id']}")
        self.assertEqual(s, 200, bike)
        self.assertEqual((bike["display_name"], bike["year_range"]), ("Kawasaki KLR650", "2008-2018"))
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT model_code, bike_type FROM bikes WHERE id=?",
                                     (d["bike_id"],)).fetchone(), ("KL650E", "Dual-sport / Adventure"))
        self.assertEqual(con.execute("SELECT status, bike_id, admin_note FROM bike_requests WHERE id=?",
                                     (rid,)).fetchone(), ("added", d["bike_id"], "Added as the 2008-2018 generation"))
        con.close()
        # the riders see where it went; deciding twice is refused
        s, b = rider.get("/api/bike-requests")
        done = next(x for x in b["decided"] if x["id"] == rid)
        self.assertEqual((done["status"], done["bike_id"], done["bike_name"]), ("added", d["bike_id"], "Kawasaki KLR650"))
        self.assertEqual(adm.post(f"/api/admin/bike-requests/{rid}/decide", {"status": "declined"})[0], 409)
        self.assertEqual(tina.post(f"/api/bike-requests/{rid}/support", {})[0], 409)

    def test_q02_a_bike_request_can_be_matched_or_declined(self):
        """Already here under another name: the request is settled as that
        bike and nothing is created. Or declined, with a note the riders
        read. Creating without years is refused -- a guessed year is wrong."""
        rider = self.as_("gp_hayes")
        adm = self.as_("admin")
        s, r = rider.post("/api/bike-requests", {"make": "Honda", "model": "Hornet 900"})
        self.assertEqual(s, 200, r)
        self.assertEqual(adm.post(f"/api/admin/bike-requests/{r['request_id']}/decide", {"status": "added"})[0], 400)
        s, q = adm.get("/api/admin/bike-requests")
        it = next(x for x in q["items"] if x["id"] == r["request_id"])
        self.assertTrue(any("CB919" in b["display_name"] or "Hornet" in b["display_name"] for b in it["maybe"]), it["maybe"])
        cb919 = it["maybe"][0]["bike_id"]
        before = sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM bikes").fetchone()[0]
        s, d = adm.post(f"/api/admin/bike-requests/{r['request_id']}/decide", {"status": "added", "bike_id": cb919})
        self.assertEqual(s, 200, d)
        self.assertEqual((d["created"], d["bike_id"]), (False, cb919))
        self.assertEqual(sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM bikes").fetchone()[0], before)

        s, r = rider.post("/api/bike-requests", {"make": "Acme", "model": "Rocket Sled", "year_from": 1999})
        self.assertEqual(s, 200, r)
        s, d = adm.post(f"/api/admin/bike-requests/{r['request_id']}/decide",
                        {"status": "declined", "note": "Not a motorcycle"})
        self.assertEqual(s, 200, d)
        s, b = self.anon().get("/api/bike-requests")
        done = next(x for x in b["decided"] if x["id"] == r["request_id"])
        self.assertEqual((done["status"], done["admin_note"], done["bike_id"]), ("declined", "Not a motorcycle", None))
        s, sm = adm.get("/api/admin/summary")
        self.assertIn("bike_requests", sm)


    def test_r01_managers_have_a_board(self):
        """Threads and replies between managers and admin; a rider has no
        way in. Unread is per reader; pin and lock are admin's; your own
        words are yours to edit, and a thread others wrote in is admin's to
        remove."""
        adm = self.as_("admin")
        b1 = self._new_bike(adm, "BOARD ONE", 2001, 2001)
        b2 = self._new_bike(adm, "BOARD TWO", 2001, 2001)
        adm.post(f"/api/admin/bikes/{b1}/manager", {"user_id": self._uid("gp_hayes")})
        adm.post(f"/api/admin/bikes/{b2}/manager", {"user_id": self._uid("m.alvarez")})
        hayes, alv, rider = self.as_("gp_hayes"), self.as_("m.alvarez"), self.as_("sohc_sam")

        self.assertEqual(rider.get("/api/board/threads")[0], 403)
        self.assertEqual(rider.post("/api/board/threads", {"title": "hi", "body": "hi"})[0], 403)
        self.assertEqual(hayes.post("/api/board/threads", {"title": "", "body": "x"})[0], 400)
        self.assertEqual(hayes.post("/api/board/threads", {"title": "x", "body": "  "})[0], 400)
        self.assertEqual(hayes.post("/api/board/threads", {"title": "x", "body": "y", "bike_id": 999999})[0], 404)
        s, r = hayes.post("/api/board/threads", {"title": "Wiring diagram for the 919?",
                                                 "body": "Anyone have it scanned? https://example.org/x", "bike_id": b1})
        self.assertEqual(s, 200, r)
        tid = r["thread_id"]
        # unread for the other manager, not for the author; opening it marks it read
        s, lst = alv.get("/api/board/threads")
        t = next(x for x in lst["threads"] if x["id"] == tid)
        self.assertEqual((t["unread"], t["posts"], t["author"], t["bike_name"]), (True, 1, "gp_hayes", "Honda BOARD ONE"))
        s, lst = hayes.get("/api/board/threads")
        self.assertFalse(next(x for x in lst["threads"] if x["id"] == tid)["unread"])
        s, th = alv.get(f"/api/board/threads/{tid}")
        self.assertEqual(s, 200, th)
        self.assertEqual((th["thread"]["title"], len(th["posts"]), th["posts"][0]["first"], th["posts"][0]["mine"]),
                         ("Wiring diagram for the 919?", 1, True, False))
        s, lst = alv.get("/api/board/threads")
        self.assertFalse(next(x for x in lst["threads"] if x["id"] == tid)["unread"])
        # a reply makes it unread for the author again, and searchable
        s, r2 = alv.post(f"/api/board/threads/{tid}/posts", {"body": "Yes -- the Haynes has it, page 212."})
        self.assertEqual(s, 200, r2)
        s, lst = hayes.get("/api/board/threads")
        t = next(x for x in lst["threads"] if x["id"] == tid)
        self.assertEqual((t["unread"], t["posts"], t["last_by"]), (True, 2, "m.alvarez"))
        s, lst = hayes.get("/api/board/threads?q=Haynes")
        self.assertEqual([x["id"] for x in lst["threads"]], [tid])
        # the nav count
        s, me = hayes.get("/api/auth/me")
        self.assertEqual(me["unread"]["board"], 1)
        s, me = rider.get("/api/auth/me")
        self.assertIsNone(me["unread"])
        # edit your own, not another's; delete a reply, never the opening post
        self.assertEqual(hayes.patch(f"/api/board/posts/{r2['post_id']}", {"body": "no"})[0], 403)
        self.assertEqual(alv.patch(f"/api/board/posts/{r2['post_id']}", {"body": "Yes -- Haynes p.212."})[0], 200)
        s, th = hayes.get(f"/api/board/threads/{tid}")
        self.assertEqual(th["posts"][1]["body"], "Yes -- Haynes p.212.")
        self.assertIsNotNone(th["posts"][1]["edited_at"])
        first = th["posts"][0]["id"]
        self.assertEqual(hayes.delete(f"/api/board/posts/{first}")[0], 409)
        # pin and lock are admin's
        self.assertEqual(hayes.post(f"/api/board/threads/{tid}/pin", {})[0], 403)
        s, r3 = adm.post(f"/api/board/threads/{tid}/pin", {})
        self.assertEqual((s, r3["pinned"]), (200, True))
        s, r3 = adm.post(f"/api/board/threads/{tid}/lock", {"on": True})
        self.assertEqual((s, r3["locked"]), (200, True))
        self.assertEqual(hayes.post(f"/api/board/threads/{tid}/posts", {"body": "more"})[0], 409)
        self.assertEqual(adm.post(f"/api/board/threads/{tid}/posts", {"body": "admin can"})[0], 200)
        s, lst = hayes.get("/api/board/threads")
        self.assertEqual(lst["threads"][0]["id"], tid)   # pinned first
        # others have replied: the author cannot delete it, admin can
        self.assertEqual(hayes.delete(f"/api/board/threads/{tid}")[0], 403)
        self.assertEqual(alv.delete(f"/api/board/posts/{r2['post_id']}")[0], 200)
        self.assertEqual(adm.delete(f"/api/board/threads/{tid}")[0], 200)
        self.assertEqual(hayes.get(f"/api/board/threads/{tid}")[0], 404)
        # a thread nobody replied to is the author's to take back
        s, r4 = hayes.post("/api/board/threads", {"title": "oops", "body": "wrong board"})
        self.assertEqual(hayes.delete(f"/api/board/threads/{r4['thread_id']}")[0], 200)
        # the members list is who can be messaged
        s, mem = hayes.get("/api/board/members")
        names = {p["username"]: p for p in mem["people"]}
        self.assertIn("admin", names)
        self.assertIn("Honda BOARD TWO", names["m.alvarez"]["bikes"])
        self.assertNotIn("sohc_sam", names)

    def test_r02_managers_can_message_each_other(self):
        """One conversation per pair; unread until the other opens it; a
        rider can neither send nor be sent to; nobody messages themselves."""
        adm = self.as_("admin")
        b1 = self._new_bike(adm, "DM ONE", 2002, 2002)
        b2 = self._new_bike(adm, "DM TWO", 2002, 2002)
        adm.post(f"/api/admin/bikes/{b1}/manager", {"user_id": self._uid("gp_hayes")})
        adm.post(f"/api/admin/bikes/{b2}/manager", {"user_id": self._uid("m.alvarez")})
        hayes, alv, rider = self.as_("gp_hayes"), self.as_("m.alvarez"), self.as_("sohc_sam")
        h_id, a_id, r_id, adm_id = self._uid("gp_hayes"), self._uid("m.alvarez"), self._uid("sohc_sam"), self._uid("admin")

        self.assertEqual(rider.get("/api/messages")[0], 403)
        self.assertEqual(hayes.post(f"/api/messages/with/{r_id}", {"body": "hi"})[0], 403)
        self.assertEqual(hayes.post(f"/api/messages/with/{h_id}", {"body": "hi"})[0], 400)
        self.assertEqual(hayes.post(f"/api/messages/with/999999", {"body": "hi"})[0], 404)
        self.assertEqual(hayes.post(f"/api/messages/with/{a_id}", {"body": "   "})[0], 400)
        s, r = hayes.post(f"/api/messages/with/{a_id}", {"body": "Got a minute? Question about your CB750."})
        self.assertEqual(s, 200, r)
        s, r2 = hayes.post(f"/api/messages/with/{a_id}", {"body": "Second one."})
        self.assertEqual(r2["conversation_id"], r["conversation_id"])
        # alvarez sees one conversation, two unread; the nav count agrees
        s, lst = alv.get("/api/messages")
        self.assertEqual(len(lst["conversations"]), 1)
        c = lst["conversations"][0]
        self.assertEqual((c["other"], c["other_id"], c["unread"], c["last_mine"], c["preview"]),
                         ("gp_hayes", h_id, 2, False, "Second one."))
        s, me = alv.get("/api/auth/me")
        self.assertEqual(me["unread"]["messages"], 2)
        # opening reads them; sender then sees "read"
        s, th = alv.get(f"/api/messages/with/{h_id}")
        self.assertEqual(s, 200, th)
        self.assertEqual([m["mine"] for m in th["messages"]], [False, False])
        self.assertEqual(alv.get("/api/messages")[1]["conversations"][0]["unread"], 0)
        s, th = hayes.get(f"/api/messages/with/{a_id}")
        self.assertTrue(all(m["mine"] and m["read_at"] for m in th["messages"]))
        # a reply the other way lands in the same conversation, unread for hayes
        s, r3 = alv.post(f"/api/messages/with/{h_id}", {"body": "Sure -- what's up?"})
        self.assertEqual(r3["conversation_id"], r["conversation_id"])
        s, lst = hayes.get("/api/messages")
        self.assertEqual((lst["conversations"][0]["unread"], lst["conversations"][0]["last_mine"]), (1, False))
        # admin is reachable; looking at an empty conversation creates nothing
        s, th = hayes.get(f"/api/messages/with/{adm_id}")
        self.assertEqual((s, th["messages"], th["other"]["username"]), (200, [], "admin"))
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM dm_conversations").fetchone()[0], 1)
        con.close()
        self.assertEqual(rider.get(f"/api/messages/with/{h_id}")[0], 403)


    def test_s01_passwords_can_be_changed_and_accounts_suspended(self):
        """Your own password needs the current one; admin can set anyone's
        and suspend anyone but themselves. A changed password ends the other
        sessions; a suspended account cannot sign in."""
        a = self.as_("sohc_sam")
        b = self.as_("sohc_sam")                       # a second device
        self.assertEqual(a.post("/api/auth/password", {"current": "wrong", "password": "longenough1"})[0], 403)
        self.assertEqual(a.post("/api/auth/password", {"current": "gearhead", "password": "short"})[0], 400)
        s, r = a.post("/api/auth/password", {"current": "gearhead", "password": "longenough1"})
        self.assertEqual(s, 200, r)
        self.assertEqual(a.get("/api/auth/me")[1]["user"]["username"], "sohc_sam")   # this device stays
        self.assertIsNone(b.get("/api/auth/me")[1]["user"])                          # the other is out
        self.assertEqual(self.anon().login("sohc_sam")[0], 401)
        self.assertEqual(self.anon().login("sohc_sam", "longenough1")[0], 200)
        # the token never leaves the server
        self.assertNotIn("token", a.get("/api/auth/me")[1]["user"])

        adm = self.as_("admin")
        uid = self._uid("sohc_sam")
        self.assertEqual(self.as_("m.alvarez").post(f"/api/admin/users/{uid}/password", {"password": "hijacked1"})[0], 403)
        s, r = adm.post(f"/api/admin/users/{uid}/password", {"password": "gearhead"})
        self.assertEqual(s, 200, r)
        self.assertEqual(self.anon().login("sohc_sam")[0], 200)
        self.assertEqual(adm.post(f"/api/admin/users/{self._uid('admin')}/suspend", {})[0], 400)
        s, r = adm.post(f"/api/admin/users/{uid}/suspend", {"suspended": True})
        self.assertEqual((s, r["suspended"]), (200, True))
        self.assertIn(self.anon().login("sohc_sam")[0], (401, 403))
        adm.post(f"/api/admin/users/{uid}/suspend", {"suspended": False})
        self.assertEqual(self.anon().login("sohc_sam")[0], 200)

    def test_s02_admin_can_download_and_restore_the_database(self):
        """A backup is a real SQLite file of what is live; a restore replaces
        the live database with an uploaded one after checking it, and signs
        everyone out. Junk is refused before anything is touched."""
        adm = self.as_("admin")
        marker = self._new_bike(adm, "BACKUP MARKER", 2020, 2020)
        s, ctype, data = self._raw_get(adm, "/api/admin/backup")
        self.assertEqual(s, 200)
        self.assertEqual(data[:16], b"SQLite format 3\x00")
        self.assertIn("sqlite", ctype)
        self.assertEqual(self._raw_get(self.as_("m.alvarez"), "/api/admin/backup")[0], 403)

        # junk, and a database that is not ours, are refused
        self.assertEqual(self._raw_post(adm, "/api/admin/restore", b"hello", "application/octet-stream")[0], 400)
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE x (y)")
        other = con.serialize() if hasattr(con, "serialize") else None
        if other:
            self.assertEqual(self._raw_post(adm, "/api/admin/restore", other, "application/octet-stream")[0], 400)

        # a bike made after the backup disappears with the restore, the marker
        # stays, and everyone is signed out
        later = self._new_bike(adm, "AFTER BACKUP", 2021, 2021)
        s, r = self._raw_post(adm, "/api/admin/restore", data, "application/octet-stream")
        self.assertEqual(s, 200, r)
        self.assertGreater(r["bikes"], 200)
        self.assertIsNone(adm.get("/api/auth/me")[1]["user"])
        adm2 = self.as_("admin")
        s, b = adm2.get(f"/api/bikes/{marker}")
        self.assertEqual(s, 200, b)
        self.assertEqual(b["model_code"], "BACKUP MARKER")
        self.assertEqual(adm2.get(f"/api/bikes/{later}")[0], 404)

        # photos travel as a zip
        s, ctype, z = self._raw_get(adm2, "/api/admin/backup-photos")
        self.assertEqual((s, z[:2]), (200, b"PK"))
        import io as _io, zipfile
        buf = _io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("7.jpg", self.JPG)
            zf.writestr("notes.txt", "not a photo")
            zf.writestr("8.png", self.JPG)          # says png, is a jpeg
        s, r = self._raw_post(adm2, "/api/admin/restore-photos", buf.getvalue(), "application/zip")
        self.assertEqual(s, 200, r)
        self.assertEqual((r["restored"], sorted(r["skipped"])), (1, ["8.png", "notes.txt"]))
        self.assertEqual(self._raw_get(self.anon(), "/photos/7.jpg")[1], "image/jpeg")


    def test_s03_admin_can_sign_in_as_a_member_and_come_back(self):
        """One browser: admin becomes a manager, sees the site as them (and
        is told so), then comes back with one call. Never as another admin,
        never as a suspended account; the way back needs a live admin
        session behind it; it is all in the audit log."""
        adm = self.as_("admin")
        b = self._new_bike(adm, "TEST AS", 2016, 2016)
        uid = self._uid("gp_hayes")
        adm.post(f"/api/admin/bikes/{b}/manager", {"user_id": uid})
        # the members list says which bikes a manager keeps
        s, r = adm.get("/api/users")
        me = next(u for u in r["users"] if u["username"] == "gp_hayes")
        self.assertIn("Honda TEST AS (2016)", me["bikes"])

        self.assertEqual(adm.post(f"/api/admin/users/{self._uid('admin')}/impersonate", {})[0], 400)
        self.assertEqual(self.as_("m.alvarez").post(f"/api/admin/users/{uid}/impersonate", {})[0], 403)
        s, r = adm.post(f"/api/admin/users/{uid}/impersonate", {})
        self.assertEqual(s, 200, r)
        self.assertEqual((r["username"], r["role"]), ("gp_hayes", "manager"))
        # the same browser is now gp_hayes, knows it is a test sign-in, and can act as them
        s, me = adm.get("/api/auth/me")
        self.assertEqual(me["user"]["username"], "gp_hayes")
        self.assertEqual(me["testing_as"], {"admin": "admin"})
        self.assertIn(b, me["manages"])
        self.assertEqual(adm.get("/api/admin/summary")[0], 403)      # really a manager now
        # and back
        s, r = adm.post("/api/auth/return")
        self.assertEqual((s, r["username"]), (200, "admin"))
        s, me = adm.get("/api/auth/me")
        self.assertEqual((me["user"]["username"], me["testing_as"]), ("admin", None))
        self.assertEqual(adm.post("/api/auth/return")[0], 403)        # nothing to return to now
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM admin_actions WHERE action='user.impersonate'").fetchone()[0], 1)
        con.close()
        # a suspended account cannot be tested as; a plain member cannot use the way back
        adm.post(f"/api/admin/users/{uid}/suspend", {"suspended": True})
        self.assertEqual(adm.post(f"/api/admin/users/{uid}/impersonate", {})[0], 409)
        adm.post(f"/api/admin/users/{uid}/suspend", {"suspended": False})
        self.assertEqual(self.as_("sohc_sam").post("/api/auth/return")[0], 403)


    def test_t01_manager_tiers_are_computed_from_the_record(self):
        """Bronze for being assigned; Silver from values, sourcing, flags
        and speed; Gold the same plus admin's say-so; an overturned-value
        rate above the cap holds a manager at Bronze. The thresholds are
        lowered for the test so a handful of rows walks the whole ladder."""
        saved = (dict(app.TIER_RULES), app.MEDIAN_MIN_FLAGS)
        app.TIER_RULES = {"silver": {"specs": 2, "confirmed_share": 50, "flags": 1, "median_days": 7.0},
                          "gold":   {"specs": 3, "confirmed_share": 60, "flags": 2, "median_days": 7.0}}
        app.MEDIAN_MIN_FLAGS = 1
        try:
            adm = self.as_("admin")
            bike = self._new_bike(adm, "TIER TEST", 2019, 2019)
            # a brand-new account, so nothing earlier in the suite colours the numbers
            mgr = self.anon()
            s, reg = mgr.post("/api/auth/register", {"username": "tier_tess", "email": "tt@example.com",
                                                     "password": "a-good-long-one"})
            self.assertEqual(s, 200, reg)
            uid = reg["user"]["id"]
            adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": uid})
            s, st = self.anon().get(f"/api/users/{uid}/standing")
            self.assertEqual(s, 200, st)
            self.assertEqual((st["tier"], st["is_manager"], st["stats"]["specs_entered"]), ("bronze", True, 0))
            # the bike's header carries the tier; a rider has none
            s, b = self.anon().get(f"/api/bikes/{bike}")
            self.assertEqual(b["managers"][0]["tier"], "bronze")
            self.assertIsNone(self.anon().get(f"/api/users/{self._uid('sohc_sam')}/standing")[1]["tier"])

            # three values, two of them from the manual
            con = sqlite3.connect(self.db)
            ids = [r[0] for r in con.execute(
                "SELECT s.id FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
                " WHERE s.bike_id=? AND s.value IS NULL AND f.value_type='text' LIMIT 3", (bike,))]
            con.close()
            self.assertEqual(len(ids), 3)
            for i, sid in enumerate(ids):
                s, r = mgr.patch(f"/api/specs/{sid}", {"value": f"v{i}", "confidence": "confirmed" if i < 2 else "mfr"})
                self.assertEqual(s, 200, r)
            # a rider flags one; the manager dismisses it (resolved, fast)
            rider = self.as_("sohc_sam")
            s, f = rider.post(f"/api/specs/{ids[0]}/flags", {"reason": "other", "detail": "?"})
            self.assertEqual(s, 200, f)
            s, r = mgr.post(f"/api/flags/{f['id']}/dismiss", {})
            self.assertEqual(s, 200, r)
            st = self.anon().get(f"/api/users/{uid}/standing")[1]
            self.assertEqual(st["stats"]["specs_entered"], 3)
            self.assertEqual(st["stats"]["confirmed_share"], 66.7)
            self.assertEqual(st["stats"]["flags_resolved"], 1)
            self.assertEqual(st["tier"], "silver")
            self.assertFalse(st["gold_eligible"])
            self.assertEqual(adm.post(f"/api/admin/users/{uid}/gold", {"confirmed": True})[0], 409)

            # a second resolved flag: the numbers are there for Gold, admin confirms
            s, f2 = rider.post(f"/api/specs/{ids[1]}/flags", {"reason": "other"})
            mgr.post(f"/api/flags/{f2['id']}/dismiss", {})
            st = self.anon().get(f"/api/users/{uid}/standing")[1]
            self.assertEqual((st["tier"], st["gold_eligible"], st["gold_confirmed"]), ("silver", True, False))
            self.assertEqual(self.as_("m.alvarez").post(f"/api/admin/users/{uid}/gold", {"confirmed": True})[0], 403)
            s, r = adm.post(f"/api/admin/users/{uid}/gold", {"confirmed": True})
            self.assertEqual((s, r["tier"]), (200, "gold"))
            s, users = adm.get("/api/users")
            self.assertEqual(next(u for u in users["users"] if u["user_id"] == uid)["tier"], "gold")

            # a wrong value: a flag the manager had to FIX counts against the author of the old value
            s, f3 = rider.post(f"/api/specs/{ids[2]}/flags", {"reason": "incorrect", "detail": "wrong"})
            s, r = mgr.post(f"/api/flags/{f3['id']}/fix", {"new_value": "v2-corrected"})
            self.assertEqual(s, 200, r)
            st = self.anon().get(f"/api/users/{uid}/standing")[1]
            self.assertEqual(st["stats"]["wrong_specs"], 1)
            self.assertGreater(st["stats"]["wrong_rate"], app.WRONG_RATE_CAP)
            self.assertEqual(st["tier"], "bronze")          # capped, Gold confirmation or not
            # badges are computed too
            names = {b["key"]: b["earned"] for b in st["badges"]}
            self.assertFalse(names["first_hundred"])
            self.assertTrue(names["clean_sheet"] is False)     # one overturned value inside 90 days
            # the founder badge is admin's to give, and shows wherever the tier does
            self.assertFalse(names["founding"])
            self.assertEqual(self.as_("m.alvarez").post(f"/api/admin/users/{uid}/founder", {"founder": True})[0], 403)
            s, r = adm.post(f"/api/admin/users/{uid}/founder", {"founder": True})
            self.assertEqual((s, r["founder"]), (200, True))
            st = self.anon().get(f"/api/users/{uid}/standing")[1]
            self.assertTrue(st["founder"])
            self.assertTrue({b["key"]: b["earned"] for b in st["badges"]}["founding"])
            self.assertTrue(self.anon().get(f"/api/bikes/{bike}")[1]["managers"][0]["founder"])
            adm.post(f"/api/admin/users/{uid}/founder", {"founder": False})
            self.assertFalse(self.anon().get(f"/api/users/{uid}/standing")[1]["founder"])
            adm.post(f"/api/admin/users/{uid}/gold", {"confirmed": False})
        finally:
            app.TIER_RULES, app.MEDIAN_MIN_FLAGS = saved[0], saved[1]


    def test_g04_photos_by_year_and_the_first_managers_photo_stays(self):
        """The main photo is the first manager's and a later manager cannot
        replace it; they add a photo for the year their own bike is, one
        year each. The bike shows the year's photo for that year and the
        main photo for the rest."""
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "YEAR PHOTOS", 2004, 2007)
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": self._uid("m.alvarez")})
        dave, alv = self.as_("cb919_dave"), self.as_("m.alvarez")
        s, b = self._raw_post(dave, f"/api/bikes/{bike}/photo", self.JPG, "image/jpeg")
        self.assertEqual(s, 200, b)
        self.assertIsNone(b["photo"]["year"])
        # the second manager cannot touch the main photo
        s, b = self._raw_post(alv, f"/api/bikes/{bike}/photo", self.PNG, "image/png")
        self.assertEqual(s, 403, b)
        self.assertIn("cb919_dave", b["error"])
        self.assertEqual(alv.delete(f"/api/bikes/{bike}/photo")[0], 403)
        # a year that is not the bike's, and a year that is
        self.assertEqual(self._raw_post(alv, f"/api/bikes/{bike}/photo?year=1999", self.PNG, "image/png")[0], 404)
        s, b = self._raw_post(alv, f"/api/bikes/{bike}/photo?year=2005", self.PNG, "image/png")
        self.assertEqual(s, 200, b)
        self.assertEqual((b["photo"]["year"], b["photo"]["uploaded_by"]), (2005, "m.alvarez"))
        self.assertTrue(b["photo"]["url"].startswith(f"/photos/{bike}-2005.png"))
        self.assertEqual(self._raw_get(self.anon(), f"/photos/{bike}-2005.png")[1], "image/png")
        # one year each: a second year is refused until the first is given up
        s, b = self._raw_post(alv, f"/api/bikes/{bike}/photo?year=2006", self.PNG, "image/png")
        self.assertEqual(s, 409, b)
        self.assertIn("2005", b["error"])
        # replacing their own year is fine; the first manager cannot remove it, admin can
        self.assertEqual(self._raw_post(alv, f"/api/bikes/{bike}/photo?year=2005", self.JPG, "image/jpeg")[0], 200)
        self.assertEqual(self._raw_get(self.anon(), f"/photos/{bike}-2005.png")[0], 404)
        self.assertEqual(dave.delete(f"/api/bikes/{bike}/photo?year=2005")[0], 403)
        # the page carries both
        s, page = self.anon().get(f"/api/bikes/{bike}")
        self.assertEqual(page["photo"]["uploaded_by"], "cb919_dave")
        self.assertEqual([(p["year"], p["uploaded_by"]) for p in page["year_photos"]], [(2005, "m.alvarez")])
        # the main photo can go without taking the year photo with it
        self.assertEqual(dave.delete(f"/api/bikes/{bike}/photo")[0], 200)
        s, page = self.anon().get(f"/api/bikes/{bike}")
        self.assertIsNone(page["photo"])
        self.assertEqual(len(page["year_photos"]), 1)
        self.assertEqual(adm.delete(f"/api/bikes/{bike}/photo?year=2005")[0], 200)
        self.assertEqual(self._raw_get(self.anon(), f"/photos/{bike}-2005.jpg")[0], 404)

    def test_t02_a_manager_can_retire_with_the_tier_they_leave_with(self):
        """Retiring writes down the tier at that moment; it needs the bikes
        handed on first; it shows on the member and on what they entered;
        it can be reversed."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "RETIRE TEST", 2010, 2010)
        c = self.anon()
        s, reg = c.post("/api/auth/register", {"username": "retiring_ray", "email": "rr@example.com",
                                               "password": "a-good-long-one"})
        uid = reg["user"]["id"]
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": uid})
        con = sqlite3.connect(self.db)
        sid = con.execute("SELECT s.id FROM specs s JOIN spec_fields f ON f.field_key=s.field_key"
                          " WHERE s.bike_id=? AND s.value IS NULL AND f.value_type='text' LIMIT 1", (bike,)).fetchone()[0]
        con.close()
        self.assertEqual(c.patch(f"/api/specs/{sid}", {"value": "ray's value", "confidence": "confirmed"})[0], 200)
        # still holds the bike: refused
        s, r = adm.post(f"/api/admin/users/{uid}/retire", {"retired": True})
        self.assertEqual(s, 409, r)
        adm.delete(f"/api/admin/bikes/{bike}/manager/{uid}")
        s, r = adm.post(f"/api/admin/users/{uid}/retire", {"retired": True})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["retired"]["tier"], "bronze")
        st = self.anon().get(f"/api/users/{uid}/standing")[1]
        self.assertEqual((st["tier"], st["is_manager"], st["retired"]["tier"]), (None, False, "bronze"))
        s, users = adm.get("/api/users")
        self.assertEqual(next(u for u in users["users"] if u["user_id"] == uid)["retired_tier"], "bronze")
        s, sheet = self.anon().get(f"/api/bikes/{bike}/specs")
        mine = next(x for cat in sheet["categories"] for x in cat["specs"] if x["id"] == sid)
        self.assertEqual(mine["entered_by_retired"], "bronze")
        self.assertEqual(adm.post(f"/api/admin/users/{self._uid('admin')}/retire", {"retired": True})[0], 400)
        s, r = adm.post(f"/api/admin/users/{uid}/retire", {"retired": False})
        self.assertEqual((s, r["retired"]), (200, None))

    def test_v03_a_hidden_alternative_is_hidden_from_riders(self):
        """A manager takes an alternative down without having to flag it
        first; a reader stops seeing it at once, the manager still does so
        they can put it back, and nothing is deleted."""
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "ALT HIDE", 2004, 2004)
        mgr, rider = self.as_("cb919_dave"), self.as_("sohc_sam")
        con = sqlite3.connect(self.db)
        sid = con.execute("SELECT s.id FROM specs s JOIN spec_fields f ON f.field_key=s.field_key"
                          " WHERE s.bike_id=? AND f.value_type='text' AND f.spec_type<>'fixed' LIMIT 1",
                          (bike,)).fetchone()[0]
        con.close()
        mgr.patch(f"/api/specs/{sid}", {"value": "the stock one"})
        s, a = rider.post(f"/api/specs/{sid}/alternates", {"text": "brown/yellow"})
        self.assertEqual(s, 200, a)
        alt_id = a["id"]

        def alts_for(client):
            _, sheet = client.get(f"/api/bikes/{bike}/specs")
            row = next(x for c in sheet["categories"] for x in c["specs"] if x["id"] == sid)
            return [(x["text"], x["paused"]) for x in row["alternates"]]
        self.assertEqual(alts_for(self.anon()), [("brown/yellow", 0)])

        # a rider cannot hide it; another bike's manager cannot; this one can
        self.assertEqual(rider.post(f"/api/alternates/{alt_id}/pause", {"paused": True})[0], 403)
        self.assertEqual(self.as_("m.alvarez").post(f"/api/alternates/{alt_id}/pause", {"paused": True})[0], 403)
        s, r = mgr.post(f"/api/alternates/{alt_id}/pause", {"paused": True})
        self.assertEqual((s, r["paused"], r["text"]), (200, True, "brown/yellow"))

        self.assertEqual(alts_for(self.anon()), [], "a hidden alternative still reached a reader")
        self.assertEqual(alts_for(rider), [])
        self.assertEqual(alts_for(mgr), [("brown/yellow", 1)], "the manager lost sight of what they hid")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM spec_alternates WHERE id=?", (alt_id,)).fetchone()[0], 1,
                         "hiding deleted the alternative")
        con.close()
        # and back
        s, r = mgr.post(f"/api/alternates/{alt_id}/pause", {"paused": False})
        self.assertEqual((s, r["paused"]), (200, False))
        self.assertEqual(alts_for(self.anon()), [("brown/yellow", 0)])

    def test_v02_admin_sets_the_example_value_a_field_shows_in_every_entry_box(self):
        """"e.g. K&N KN-145" on the field reaches the spec on every bike, so
        people entering a value can see the shape it should take. It is an
        example, never a value: the spec stays empty."""
        adm = self.as_("admin")
        bike = self._identity_bike(adm, "EXAMPLE TEST", 2003, 2003)
        con = sqlite3.connect(self.db)
        key, wire = con.execute(
            "SELECT s.field_key, (SELECT field_key FROM spec_fields WHERE value_type='wire_color' LIMIT 1)"
            " FROM specs s JOIN spec_fields f ON f.field_key = s.field_key"
            " WHERE s.bike_id=? AND f.value_type='text' LIMIT 1", (bike,)).fetchone()
        con.close()
        self.assertEqual(self.as_("cb919_dave").patch(f"/api/admin/fields/{key}/example", {"example": "x"})[0], 403)
        self.assertEqual(adm.patch("/api/admin/fields/no_such/example", {"example": "x"})[0], 404)
        self.assertEqual(adm.patch(f"/api/admin/fields/{key}/example", {"example": "y" * 121})[0], 400)
        s, r = adm.patch(f"/api/admin/fields/{key}/example", {"example": "K&N KN-145"})
        self.assertEqual((s, r["example"]), (200, "K&N KN-145"))
        _, sheet = self.anon().get(f"/api/bikes/{bike}/specs")
        row = next(x for c in sheet["categories"] for x in c["specs"] if x["field_key"] == key)
        self.assertEqual(row["example"], "K&N KN-145")
        self.assertIsNone(row["value"], "the example leaked into the value")
        # a field picked from a list would never show it
        if wire:
            self.assertEqual(adm.patch(f"/api/admin/fields/{wire}/example", {"example": "Y/R"})[0], 409)
        s, r = adm.patch(f"/api/admin/fields/{key}/example", {"example": ""})
        self.assertIsNone(r["example"])

    def test_v01_notes_on_a_spec_per_bike_and_across_every_bike(self):
        """A manager writes a note on their own bike's spec; admin writes
        one on the field, which shows on every bike carrying it. Both land
        on the sheet, and neither is the value."""
        adm = self.as_("admin")
        b1 = self._identity_bike(adm, "NOTE ONE", 2001, 2001)
        b2 = self._identity_bike(adm, "NOTE TWO", 2002, 2002)
        mgr = self.as_("cb919_dave")
        key = "valve_clearance_intake"
        con = sqlite3.connect(self.db)
        has = con.execute("SELECT 1 FROM specs WHERE bike_id=? AND field_key=?", (b1, key)).fetchone()
        if not has:
            key = con.execute("SELECT field_key FROM specs WHERE bike_id=? LIMIT 1", (b1,)).fetchone()[0]
        con.close()

        # a rider cannot write one; the bike's manager can
        self.assertEqual(self.as_("sohc_sam").patch(f"/api/bikes/{b1}/specs/{key}/note", {"body": "x"})[0], 403)
        self.assertEqual(mgr.patch(f"/api/bikes/{b1}/specs/no_such_field/note", {"body": "x"})[0], 404)
        s, r = mgr.patch(f"/api/bikes/{b1}/specs/{key}/note", {"body": "Measure cold, bike upright."})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["note"]["body"], "Measure cold, bike upright.")
        self.assertEqual(mgr.patch(f"/api/bikes/{b1}/specs/{key}/note", {"body": "x" * 501})[0], 400)

        def note_on(bike):
            _, sheet = self.anon().get(f"/api/bikes/{bike}/specs")
            row = next(x for c in sheet["categories"] for x in c["specs"] if x["field_key"] == key)
            return row["note"], row["site_note"]
        self.assertEqual(note_on(b1), ("Measure cold, bike upright.", None))
        self.assertEqual(note_on(b2), (None, None))          # the other bike is untouched

        # admin's note on the field reaches every bike
        self.assertEqual(mgr.patch(f"/api/admin/fields/{key}/note", {"body": "no"})[0], 403)
        s, r = adm.patch(f"/api/admin/fields/{key}/note", {"body": "The manual's figure is for a cold engine."})
        self.assertEqual(s, 200, r)
        self.assertGreater(r["bikes"], 1)
        self.assertEqual(note_on(b1), ("Measure cold, bike upright.", "The manual's figure is for a cold engine."))
        self.assertEqual(note_on(b2)[1], "The manual's figure is for a cold engine.")
        # writing again replaces rather than piling up, and empty clears
        adm.patch(f"/api/admin/fields/{key}/note", {"body": "Cold engine."})
        self.assertEqual(note_on(b2)[1], "Cold engine.")
        con = sqlite3.connect(self.db)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM spec_notes WHERE bike_id IS NULL AND field_key=?",
                                     (key,)).fetchone()[0], 1)
        con.close()
        self.assertEqual(adm.patch(f"/api/admin/fields/{key}/note", {"body": ""})[1]["note"], None)
        self.assertEqual(note_on(b1)[1], None)
        self.assertEqual(mgr.patch(f"/api/bikes/{b1}/specs/{key}/note", {"body": ""})[1]["note"], None)
        self.assertEqual(note_on(b1)[0], None)

    def test_u01_the_model_dropdown_says_what_the_bike_is_called(self):
        """A model code like FLFBS means nothing to a reader; the option
        shows the name with the code beside it, and just the code where
        the name already carries it."""
        adm = self.as_("admin")
        s, b = adm.post("/api/bikes", {"make": "Harley-Davidson", "model_code": "FLFBS TEST",
                                       "year_start": 2018, "year_end": 2020, "name": "Harley-Davidson Fat Boy 114"})
        self.assertEqual(s, 200, b)
        s, f = self.anon().get("/api/catalog/filters?make=Harley-Davidson")
        opt = next(m for m in f["models"] if m["model_code"] == "FLFBS TEST")
        self.assertEqual(opt["label"], "Fat Boy 114 · FLFBS TEST")
        # a name that already carries the code is the label on its own
        s, b2 = adm.post("/api/bikes", {"make": "Honda", "model_code": "CB919 TEST", "year_start": 2002})
        s, f = self.anon().get("/api/catalog/filters?make=Honda")
        opt = next(m for m in f["models"] if m["model_code"] == "CB919 TEST")
        self.assertEqual(opt["label"], "CB919 TEST")

    def test_t03_the_first_manager_is_the_bikes_lead_manager_for_good(self):
        """The first person assigned to a bike is its lead manager; a
        later manager is a manager; the lead stays named on the bike after
        handing it on; admin can move it."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "LEAD TEST", 2012, 2012)
        first, second = self._uid("gp_hayes"), self._uid("m.alvarez")
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": first})
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": second})
        s, b = self.anon().get(f"/api/bikes/{bike}")
        self.assertEqual((b["lead_manager"]["username"], b["lead_manager"]["current"]), ("gp_hayes", True))
        self.assertEqual([(m["username"], m["lead"]) for m in b["managers"]], [("gp_hayes", True), ("m.alvarez", False)])
        st = self.anon().get(f"/api/users/{first}/standing")[1]
        self.assertIn(bike, [x["bike_id"] for x in st["lead_of"]])
        self.assertTrue(next(x for x in st["bikes"] if x["bike_id"] == bike)["lead"])
        # handed on: still named, no longer current
        adm.delete(f"/api/admin/bikes/{bike}/manager/{first}")
        s, b = self.anon().get(f"/api/bikes/{bike}")
        self.assertEqual((b["lead_manager"]["username"], b["lead_manager"]["current"]), ("gp_hayes", False))
        self.assertEqual([m["username"] for m in b["managers"]], ["m.alvarez"])
        # admin can move it
        self.assertEqual(self.as_("m.alvarez").post(f"/api/admin/bikes/{bike}/lead", {"user_id": second})[0], 403)
        s, r = adm.post(f"/api/admin/bikes/{bike}/lead", {"user_id": second})
        self.assertEqual(s, 200, r)
        self.assertEqual(self.anon().get(f"/api/bikes/{bike}")[1]["lead_manager"]["username"], "m.alvarez")


    def test_j03_a_rider_can_ask_for_several_specs_at_once(self):
        """One read down the tree turns up three things the bike should list
        and does not. They travel as one ask, sharing the one reason typed;
        a spec already asked for, or already on the sheet, is skipped by name
        rather than sinking the others."""
        adm = self.as_("admin")
        bike = self._new_bike(adm, "FIELDREQ MANY", 2006, 2007)
        rider = self.as_("sohc_sam")
        s, b = rider.get(f"/api/bikes/{bike}/field-requests")
        want = [f["field_key"] for f in b["fields"] if f["status"] == "missing"][:3]
        self.assertEqual(len(want), 3, "a fresh bike is missing most of the tree")
        online = next(f["field_key"] for f in b["fields"] if f["status"] == "online")

        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_keys": want, "reasoning": "rebuilding it over winter"})
        self.assertEqual(s, 200, r)
        self.assertEqual(r["count"], 3)
        self.assertEqual([a["field_key"] for a in r["asked"]], want)
        self.assertEqual(r["skipped"], [])
        s, b = rider.get(f"/api/bikes/{bike}/field-requests")
        state = {f["field_key"]: f for f in b["fields"]}
        for k in want:
            self.assertEqual((state[k]["requests"], state[k]["mine"]), (1, 1), k)

        # one already asked for, one already on the sheet, one new: the new one
        # goes, the other two come back saying why
        fresh = next(f["field_key"] for f in b["fields"]
                     if f["status"] == "missing" and not f["mine"])
        s, r = rider.post(f"/api/bikes/{bike}/field-requests",
                          {"field_keys": [want[0], online, fresh]})
        self.assertEqual(s, 200, r)
        self.assertEqual([a["field_key"] for a in r["asked"]], [fresh])
        self.assertEqual({k["field_key"] for k in r["skipped"]}, {want[0], online})

        # nothing askable at all is still a refusal, and the list has a ceiling
        self.assertEqual(rider.post(f"/api/bikes/{bike}/field-requests",
                                    {"field_keys": [want[0], online]})[0], 409)
        self.assertEqual(rider.post(f"/api/bikes/{bike}/field-requests",
                                    {"field_keys": []})[0], 400)
        self.assertEqual(rider.post(f"/api/bikes/{bike}/field-requests",
                                    {"field_keys": [f"made_up_{i}" for i in range(26)]})[0], 400)
        # a single key answers exactly as it always did
        s, b = rider.get(f"/api/bikes/{bike}/field-requests")
        solo = next(f["field_key"] for f in b["fields"]
                    if f["status"] == "missing" and not f["mine"])
        s, r = rider.post(f"/api/bikes/{bike}/field-requests", {"field_key": solo})
        self.assertEqual((s, r["kind"], r["requests"]), (200, "request", 1))


    # -- one front brake pad spec per bike --------------------------------
    SINGLE_DISC_ANSWERS = dict(CB750_ANSWERS, q20="A")
    SINGLE_DISC_ANSWERS.pop("q20a")          # q20a is only asked for twin discs

    def test_w01_a_bike_lists_one_front_brake_pad_spec_not_two(self):
        """The tree carries five front-pad fields, one per kind of front brake.
        A bike gets exactly the one its answers call for.

        In production three rows in field_triggers pointed at the wrong answer,
        and since a bike collects fields from the questionnaire AND from
        field_triggers, the wrong pad field landed on top of the right one --
        "Front Left Brake Pads" and "Front Brake Pad Left" on the same bike.
        This builds that state on purpose and then runs the migration over it.
        """
        import migrate_front_brake_pads as mig
        adm = self.as_("admin")

        # single front disc, left: one pad field, the left one
        one_disc = self._new_bike(adm, "ONE DISC TEST", 1981, 1982)
        s, _ = adm.post(f"/api/questionnaire/{one_disc}/build",
                        {"answers": self.SINGLE_DISC_ANSWERS})
        self.assertEqual(s, 200)
        # twin discs sharing one pad part: one pad field, the shared one
        twin = self._new_bike(adm, "TWIN DISC TEST", 1983, 1984)
        adm.post(f"/api/questionnaire/{twin}/build", {"answers": self.CB750_ANSWERS})

        def pads(bike):
            con = sqlite3.connect(self.db)
            got = {r[0] for r in con.execute(
                "SELECT field_key FROM specs WHERE bike_id=? AND field_key IN"
                " ('front_left_brake_pads','front_right_brake_pads','front_brake_pads',"
                "  'front_brake_pad_left','front_brake_pad_right')", (bike,))}
            con.close()
            return got

        self.assertEqual(pads(one_disc), {"front_left_brake_pads"})
        self.assertEqual(pads(twin), {"front_brake_pads"})

        # now break it the way production was broken, and add a third bike whose
        # duplicate somebody has actually filled in
        filled = self._new_bike(adm, "FILLED DUPE TEST", 1985, 1986)
        adm.post(f"/api/questionnaire/{filled}/build",
                 {"answers": self.SINGLE_DISC_ANSWERS})
        con = sqlite3.connect(self.db)
        con.execute("INSERT OR IGNORE INTO field_triggers (field_key, question_id, option_label)"
                    " VALUES ('front_brake_pad_left','q20','A')")
        con.execute("INSERT OR IGNORE INTO field_triggers (field_key, question_id, option_label)"
                    " VALUES ('front_left_brake_pads','q20','C')")
        for b in (one_disc, filled):
            con.execute("INSERT INTO specs (bike_id, field_key, value, confidence)"
                        " VALUES (?,'front_brake_pad_left',NULL,'pending')", (b,))
        con.execute("INSERT INTO specs (bike_id, field_key, value, confidence)"
                    " VALUES (?,'front_left_brake_pads','EBC FA142HH','pending')", (twin,))
        con.commit(); con.close()
        self.assertEqual(pads(one_disc), {"front_left_brake_pads", "front_brake_pad_left"})
        self.assertEqual(len(pads(twin)), 2)

        mig.migrate(self.db)

        # the empty duplicates are gone, each bike keeps the right one
        self.assertEqual(pads(one_disc), {"front_left_brake_pads"})
        self.assertEqual(pads(filled), {"front_left_brake_pads"})
        # the one somebody filled in is KEPT -- a sourced value is not the
        # migration's to throw away, it is reported for a person to look at
        self.assertEqual(pads(twin), {"front_brake_pads", "front_left_brake_pads"})

        con = sqlite3.connect(self.db)
        trig = {(r[0], r[1], r[2]) for r in con.execute(
            "SELECT field_key, question_id, option_label FROM field_triggers"
            " WHERE field_key LIKE '%brake_pad%'")}
        con.close()
        self.assertIn(("front_brake_pad_left", "q20a", "B"), trig,
                      "the left/right pair belongs on 'twin discs, different pads'")
        self.assertNotIn(("front_brake_pad_left", "q20", "A"), trig)
        self.assertNotIn(("front_left_brake_pads", "q20", "C"), trig)

        # and running it again does nothing
        mig.migrate(self.db)
        self.assertEqual(pads(one_disc), {"front_left_brake_pads"})


    # -- every value says where it came from ------------------------------
    def test_w02_a_value_nobody_typed_says_so(self):
        """5,452 of the values on the live site were seeded from published
        model lists, not entered by a rider, and the page showed nothing at
        all beside them -- which reads as the site's own figure. Each one now
        names the import, and the moment a rider writes over it the value is
        theirs and the import is no longer credited.
        """
        import migrate_value_source as mig
        adm = self.as_("admin")
        bike = self._new_bike(adm, "SEEDED VALUE TEST", 1996, 1997)
        adm.post(f"/api/admin/bikes/{bike}/manager", {"user_id": self._uid("gp_hayes")})

        def sheet():
            s, d = self.anon().get(f"/api/bikes/{bike}/specs")
            self.assertEqual(s, 200, d)
            return {x["field_key"]: x for c in d["categories"] for x in c["specs"]}

        key = next(k for k, v in sheet().items()
                   if v["value"] is None and v["value_type"] == "text")

        # the import, as it really ran: a value with nobody's name on it
        con = sqlite3.connect(self.db)
        con.execute("UPDATE specs SET value='14T', entered_by=NULL, value_source=NULL"
                    " WHERE bike_id=? AND field_key=?", (bike, key))
        con.commit(); con.close()
        row = sheet()[key]
        self.assertEqual((row["value"], row["entered_by_username"], row["value_source"]),
                         ("14T", None, None), "the state the migration has to find")

        mig.migrate(self.db)
        row = sheet()[key]
        self.assertEqual(row["value_source"], "catalogue")
        self.assertIsNone(row["entered_by_username"], "the import has no author to name")
        self.assertTrue(row["value_at"], "and it says when, because the page promises when")

        # A seeded slot is not blank, so "add the stock value" refuses it the
        # same as any other filled spec: it is corrected, not filled.
        mgr = self.as_("gp_hayes")
        self.assertEqual(mgr.post(f"/api/specs/{row['id']}/value", {"value": "15T"})[0], 409)

        # Confirming it WITHOUT changing it leaves the origin alone. The
        # manager vouched for the import; they did not become its author, and
        # erasing the source here would leave the value saying nothing at all.
        s, r = mgr.patch(f"/api/specs/{row['id']}", {"confidence": "mfr"})
        self.assertEqual(s, 200, r)
        row = sheet()[key]
        self.assertEqual((row["value"], row["entered_by_username"], row["value_source"]),
                         ("14T", None, "catalogue"))

        # Changing it takes it: the value is now the manager's word, not the list's
        s, r = mgr.patch(f"/api/specs/{row['id']}", {"value": "15T"})
        self.assertEqual(s, 200, r)
        row = sheet()[key]
        self.assertEqual((row["value"], row["entered_by_username"], row["value_source"]),
                         ("15T", "gp_hayes", None))

        # and a value a person typed is never relabelled as seeded
        mig.migrate(self.db)
        self.assertIsNone(sheet()[key]["value_source"])

        # nothing with a value is left saying nothing at all
        con = sqlite3.connect(self.db)
        orphans = con.execute(
            "SELECT COUNT(*) FROM specs WHERE value IS NOT NULL AND value <> ''"
            "  AND entered_by IS NULL AND value_source IS NULL").fetchone()[0]
        con.close()
        self.assertEqual(orphans, 0, "a value with neither an author nor a source")


if __name__ == "__main__":
    unittest.main(verbosity=2)
