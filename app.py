"""
Bike Spec Platform — integration backend.
Pure Python standard library (no pip installs needed). Run with: python3 app.py
Serves the REST API on /api/* and the static HTML pages on /.
"""
import json
import os
import sqlite3
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

DB_PATH = os.path.join(os.path.dirname(__file__), "data.db")
REPO_ROOT = os.path.dirname(__file__)
STATIC_DIR = os.path.join(REPO_ROOT, "static")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row):
    return {k: row[k] for k in row.keys()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # quiet

    def _send(self, status, payload, content_type="application/json"):
        body = json.dumps(payload).encode() if content_type == "application/json" else payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path.startswith("/api/"):
            return self._route_get(path, qs)

        # static file serving — check the static/ folder first, then fall back
        # to the repo root (in case files were uploaded flat instead of into
        # static/, which can happen with GitHub's web upload UI).
        rel = path.lstrip("/") or "honda-browser.html"
        candidates = [
            os.path.normpath(os.path.join(STATIC_DIR, rel)),
            os.path.normpath(os.path.join(REPO_ROOT, rel)),
        ]
        for full in candidates:
            if not (full.startswith(STATIC_DIR) or full.startswith(REPO_ROOT)):
                continue
            if os.path.isfile(full):
                ctype = "text/html"
                if full.endswith(".js"):
                    ctype = "application/javascript"
                elif full.endswith(".css"):
                    ctype = "text/css"
                with open(full, "rb") as f:
                    return self._send(200, f.read(), content_type=ctype)
        return self._send(404, {"error": "not found", "looked_in": candidates})

    def _route_get(self, path, qs):
        conn = db()
        try:
            if path == "/api/bikes":
                rows = conn.execute("SELECT * FROM bikes ORDER BY cc").fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            m = re.match(r"^/api/bikes/(\d+)$", path)
            if m:
                bid = int(m.group(1))
                bike = conn.execute("SELECT * FROM bikes WHERE id=?", (bid,)).fetchone()
                if not bike:
                    return self._send(404, {"error": "bike not found"})
                return self._send(200, row_to_dict(bike))

            m = re.match(r"^/api/bikes/(\d+)/specs$", path)
            if m:
                bid = int(m.group(1))
                specs = conn.execute("SELECT * FROM bike_specs WHERE bike_id=?", (bid,)).fetchall()
                out = []
                for s in specs:
                    sd = row_to_dict(s)
                    alts = conn.execute("SELECT * FROM alternates WHERE spec_id=? ORDER BY votes DESC",
                                         (s["id"],)).fetchall()
                    sd["alternates"] = [row_to_dict(a) for a in alts]
                    out.append(sd)
                return self._send(200, out)

            if path == "/api/managers":
                rows = conn.execute("SELECT * FROM bike_managers").fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            if path == "/api/users":
                # Stable numeric IDs for every user — the recommended way to
                # reference a user going forward, since usernames (e.g. "M.
                # Alvarez") contain spaces/periods that are fragile in URLs.
                rows = conn.execute("SELECT id, username, role FROM users ORDER BY username").fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            m = re.match(r"^/api/bike-managers/(\d+)/flags$", path)
            if m:
                # ID-based equivalent of /api/managers/<username>/flags.
                mgr_id = int(m.group(1))
                mgr = conn.execute("SELECT username FROM bike_managers WHERE id=?", (mgr_id,)).fetchone()
                if not mgr:
                    return self._send(404, {"error": "manager id not found"})
                rows = conn.execute("""
                    SELECT vf.*, bs.label as spec_label, bs.stock_value as current_value, bs.bike_id
                    FROM value_flags vf JOIN bike_specs bs ON vf.spec_id = bs.id
                    JOIN bike_managers bm ON bm.bike_id = bs.bike_id
                    WHERE bm.username=? AND vf.status='open'
                    ORDER BY vf.created_at DESC
                """, (mgr["username"],)).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            m = re.match(r"^/api/users/(\d+)/profile$", path)
            if m:
                # ID-based equivalent of /api/users/<username>/profile.
                uid = int(m.group(1))
                user = conn.execute("SELECT username FROM users WHERE id=?", (uid,)).fetchone()
                if not user:
                    return self._send(404, {"error": "user id not found"})
                path = f"/api/users/{user['username']}/profile"
                # fall through to the username-based handler below via recursion
                return self._route_get(path, qs)

            m = re.match(r"^/api/managers/([^/]+)/flags$", path)
            if m:
                username = unquote(m.group(1))
                rows = conn.execute("""
                    SELECT vf.*, bs.label as spec_label, bs.stock_value as current_value, bs.bike_id
                    FROM value_flags vf JOIN bike_specs bs ON vf.spec_id = bs.id
                    JOIN bike_managers bm ON bm.bike_id = bs.bike_id
                    WHERE bm.username=? AND vf.status='open'
                    ORDER BY vf.created_at DESC
                """, (username,)).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            m = re.match(r"^/api/managers/([^/]+)/not-sure$", path)
            if m:
                username = unquote(m.group(1))
                rows = conn.execute("""
                    SELECT nsa.* FROM not_sure_answers nsa WHERE nsa.submitted_by=?
                    ORDER BY nsa.created_at DESC
                """, (username,)).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            m = re.match(r"^/api/managers/([^/]+)/proposals$", path)
            if m:
                username = unquote(m.group(1))
                rows = conn.execute("SELECT * FROM branch_proposals WHERE proposed_by=? ORDER BY created_at DESC",
                                     (username,)).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            m = re.match(r"^/api/users/([^/]+)/profile$", path)
            if m:
                username = unquote(m.group(1))
                specs_entered = conn.execute(
                    "SELECT COUNT(*) c FROM value_flags WHERE entered_by=?", (username,)).fetchone()["c"]
                # count distinct specs this user entered (approx via alternates + being an entered_by on flags)
                specs_flagged = conn.execute(
                    "SELECT COUNT(*) c FROM value_flags WHERE entered_by=?", (username,)).fetchone()["c"]
                flags_submitted = conn.execute(
                    "SELECT COUNT(*) c FROM value_flags WHERE flagged_by=?", (username,)).fetchone()["c"]
                alt_count = conn.execute(
                    "SELECT COUNT(*) c FROM alternates WHERE submitted_by=?", (username,)).fetchone()["c"]
                votes_given = conn.execute(
                    "SELECT COUNT(*) c FROM votes_log WHERE username=?", (username,)).fetchone()["c"]
                return self._send(200, {
                    "username": username,
                    "specs_entered": alt_count,
                    "specs_flagged": specs_flagged,
                    "flags_submitted": flags_submitted,
                    "thumbs_up_given": votes_given,
                    "garage": 0,  # not modeled yet — flagged as an open gap
                })

            if path == "/api/admin/tree-flags":
                rows = conn.execute("""
                    SELECT tf.*, b.model, b.year FROM tree_flags tf
                    JOIN bikes b ON b.id = tf.bike_id WHERE tf.status='open'
                    ORDER BY tf.created_at DESC
                """).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            if path == "/api/admin/not-sure":
                rows = conn.execute("""
                    SELECT nsa.*, b.model, b.year FROM not_sure_answers nsa
                    JOIN bikes b ON b.id = nsa.bike_id WHERE nsa.status='pending'
                    ORDER BY nsa.created_at DESC
                """).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            if path == "/api/admin/proposals":
                rows = conn.execute("""
                    SELECT bp.*, b.model, b.year FROM branch_proposals bp
                    LEFT JOIN bikes b ON b.id = bp.bike_id WHERE bp.status='pending'
                    ORDER BY bp.created_at DESC
                """).fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            if path == "/api/admin/user-flags":
                rows = conn.execute("SELECT * FROM user_flags WHERE status='open' ORDER BY created_at DESC").fetchall()
                return self._send(200, [row_to_dict(r) for r in rows])

            return self._send(404, {"error": "unknown route", "path": path})
        finally:
            conn.close()

    # --------------------------------------------------------------- POST
    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        body = self._read_json()
        conn = db()
        try:
            m = re.match(r"^/api/specs/(\d+)/alternates$", path)
            if m:
                spec_id = int(m.group(1))
                text = (body.get("text") or "").strip()
                submitted_by = body.get("submitted_by", "anonymous")
                if not text:
                    return self._send(400, {"error": "text required"})
                cur = conn.execute("INSERT INTO alternates (spec_id, text, submitted_by) VALUES (?,?,?)",
                                    (spec_id, text, submitted_by))
                conn.commit()
                return self._send(201, {"id": cur.lastrowid})

            m = re.match(r"^/api/alternates/(\d+)/vote$", path)
            if m:
                alt_id = int(m.group(1))
                username = body.get("username", "anonymous")
                conn.execute("UPDATE alternates SET votes = votes + 1 WHERE id=?", (alt_id,))
                conn.execute("INSERT INTO votes_log (alternate_id, username) VALUES (?,?)", (alt_id, username))
                conn.commit()
                return self._send(200, {"ok": True})

            m = re.match(r"^/api/specs/(\d+)/request$", path)
            if m:
                spec_id = int(m.group(1))
                conn.execute("UPDATE bike_specs SET request_count = request_count + 1 WHERE id=?", (spec_id,))
                conn.commit()
                row = conn.execute("SELECT request_count FROM bike_specs WHERE id=?", (spec_id,)).fetchone()
                return self._send(200, {"request_count": row["request_count"]})

            m = re.match(r"^/api/specs/(\d+)/flag$", path)
            if m:
                spec_id = int(m.group(1))
                spec = conn.execute("SELECT * FROM bike_specs WHERE id=?", (spec_id,)).fetchone()
                if not spec:
                    return self._send(404, {"error": "spec not found"})
                conn.execute("""INSERT INTO value_flags (spec_id, entered_by, flagged_by, reason, detail, old_value)
                                 VALUES (?,?,?,?,?,?)""",
                             (spec_id, body.get("entered_by"), body.get("flagged_by", "anonymous"),
                              body.get("reason", "other"), body.get("detail"), spec["stock_value"]))
                conn.execute("UPDATE bike_specs SET flags = flags + 1 WHERE id=?", (spec_id,))
                conn.commit()
                return self._send(201, {"ok": True})

            m = re.match(r"^/api/flags/(\d+)/fix$", path)
            if m:
                flag_id = int(m.group(1))
                new_value = body.get("new_value", "").strip()
                flag = conn.execute("SELECT * FROM value_flags WHERE id=?", (flag_id,)).fetchone()
                if not flag:
                    return self._send(404, {"error": "flag not found"})
                conn.execute("UPDATE bike_specs SET stock_value=? WHERE id=?", (new_value, flag["spec_id"]))
                conn.execute("""UPDATE value_flags SET status='fixed', new_value=?, resolved_at=CURRENT_TIMESTAMP
                                 WHERE id=?""", (new_value, flag_id))
                conn.commit()
                return self._send(200, {"ok": True})

            m = re.match(r"^/api/flags/(\d+)/dismiss$", path)
            if m:
                flag_id = int(m.group(1))
                conn.execute("UPDATE value_flags SET status='dismissed', resolved_at=CURRENT_TIMESTAMP WHERE id=?",
                             (flag_id,))
                conn.commit()
                return self._send(200, {"ok": True})

            if path == "/api/tree-flags":
                conn.execute("""INSERT INTO tree_flags (spec_id, bike_id, question_text, comment)
                                 VALUES (?,?,?,?)""",
                             (body.get("spec_id"), body.get("bike_id"), body.get("question_text"),
                              body.get("comment", "")))
                conn.commit()
                return self._send(201, {"ok": True})

            m = re.match(r"^/api/admin/tree-flags/(\d+)/resolve$", path)
            if m:
                conn.execute("UPDATE tree_flags SET status='resolved' WHERE id=?", (int(m.group(1)),))
                conn.commit()
                return self._send(200, {"ok": True})

            if path == "/api/not-sure":
                conn.execute("""INSERT INTO not_sure_answers (bike_id, question_text, submitted_by)
                                 VALUES (?,?,?)""",
                             (body.get("bike_id"), body.get("question_text"), body.get("submitted_by")))
                conn.commit()
                return self._send(201, {"ok": True})

            m = re.match(r"^/api/admin/not-sure/(\d+)/confirm$", path)
            if m:
                conn.execute("UPDATE not_sure_answers SET status='confirmed' WHERE id=?", (int(m.group(1)),))
                conn.commit()
                return self._send(200, {"ok": True})

            if path == "/api/proposals":
                conn.execute("""INSERT INTO branch_proposals (field_name, category, bike_id, proposed_by, reasoning)
                                 VALUES (?,?,?,?,?)""",
                             (body.get("field_name"), body.get("category"), body.get("bike_id"),
                              body.get("proposed_by"), body.get("reasoning")))
                conn.commit()
                return self._send(201, {"ok": True})

            m = re.match(r"^/api/admin/proposals/(\d+)/(approve|reject)$", path)
            if m:
                pid, action = int(m.group(1)), m.group(2)
                status = "approved" if action == "approve" else "rejected"
                conn.execute("UPDATE branch_proposals SET status=? WHERE id=?", (status, pid))
                conn.commit()
                return self._send(200, {"ok": True})

            if path == "/api/user-flags":
                conn.execute("""INSERT INTO user_flags (flagged_user, flagged_by, reason, detail)
                                 VALUES (?,?,?,?)""",
                             (body.get("flagged_user"), body.get("flagged_by", "anonymous"),
                              body.get("reason", "other"), body.get("detail")))
                conn.commit()
                return self._send(201, {"ok": True})

            if path == "/api/bikes":
                # Questionnaire wizard completion -> create a new bike + its triggered specs
                cur = conn.execute("INSERT INTO bikes (make, model, year, cc, bike_type) VALUES (?,?,?,?,?)",
                                    (body.get("make", "Honda"), body.get("model", "Unknown"),
                                     body.get("year"), body.get("cc"), body.get("bike_type")))
                bike_id = cur.lastrowid
                for field in body.get("specs", []):
                    conn.execute("INSERT INTO bike_specs (bike_id, category, label, stock_value) VALUES (?,?,?,?)",
                                 (bike_id, field.get("category"), field.get("label"), field.get("value")))
                conn.commit()
                return self._send(201, {"bike_id": bike_id})

            return self._send(404, {"error": "unknown route", "path": path})
        finally:
            conn.close()


def main():
    port = int(os.environ.get("PORT", 8420))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Bike Spec Platform running at http://localhost:{port}")
    print(f"Database: {DB_PATH}")
    server.serve_forever()


if __name__ == "__main__":
    main()
