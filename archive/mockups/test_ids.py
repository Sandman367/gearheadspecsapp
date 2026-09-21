import threading, time, json, urllib.request, urllib.error
from urllib.parse import quote
import app as appmod

server = appmod.ThreadingHTTPServer(("127.0.0.1", 8421), appmod.Handler)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(0.3)

def req(method, path, body=None):
    url = f"http://127.0.0.1:8421{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

print("=== GET /api/users (new stable ID list) ===")
status, users = req("GET", "/api/users")
print(status, users)

alvarez = [u for u in users if u["username"] == "M. Alvarez"][0]
print("\n=== GET /api/users/<id>/profile (ID-based) ===")
status, profile = req("GET", f"/api/users/{alvarez['id']}/profile")
print(status, profile)

print("\n=== Compare against username-based route (should match) ===")
status2, profile2 = req("GET", f"/api/users/{quote('M. Alvarez')}/profile")
print(status2, profile2)
print("MATCH:", profile == profile2)

print("\n=== GET /api/bike-managers (to find manager id) ===")
status, mgrs = req("GET", "/api/managers")
print(status, mgrs)
alvarez_mgr_id = [m for m in mgrs if m["username"] == "M. Alvarez"][0]["id"]

print(f"\n=== GET /api/bike-managers/{alvarez_mgr_id}/flags (ID-based) ===")
status, flags = req("GET", f"/api/bike-managers/{alvarez_mgr_id}/flags")
print(status, "flag count:", len(flags))

print("\n=== GET nonexistent id (should 404 cleanly) ===")
status, err = req("GET", "/api/users/9999/profile")
print(status, err)

server.shutdown()
