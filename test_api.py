import threading, time, json, urllib.request, urllib.error
import app as appmod

server = appmod.ThreadingHTTPServer(("127.0.0.1", 8420), appmod.Handler)
t = threading.Thread(target=server.serve_forever, daemon=True)
t.start()
time.sleep(0.3)

from urllib.parse import quote

def req(method, path, body=None):
    url = f"http://127.0.0.1:8420{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

print("=== GET /api/bikes ===")
status, bikes = req("GET", "/api/bikes")
print(status, bikes)

cb919_id = [b["id"] for b in bikes if b["model"] == "CB900F2 919"][0]

print(f"\n=== GET /api/bikes/{cb919_id}/specs ===")
status, specs = req("GET", f"/api/bikes/{cb919_id}/specs")
print(status, "spec count:", len(specs))
print("sample spec:", specs[0])

print("\n=== GET manager flags for M. Alvarez ===")
status, flags = req("GET", f"/api/managers/{quote("M. Alvarez")}/flags")
print(status, flags)

if flags:
    flag_id = flags[0]["id"]
    print(f"\n=== POST fix flag {flag_id} ===")
    status, res = req("POST", f"/api/flags/{flag_id}/fix", {"new_value": "EBC FA187HH — Double-H Sintered"})
    print(status, res)

print("\n=== GET admin not-sure queue ===")
status, ns = req("GET", "/api/admin/not-sure")
print(status, ns)

print("\n=== GET admin proposals ===")
status, props = req("GET", "/api/admin/proposals")
print(status, props)

print("\n=== POST new alternate ===")
target_spec = specs[1]["id"]
status, res = req("POST", f"/api/specs/{target_spec}/alternates", {"text": "Test Alternate Value", "submitted_by": "test_user"})
print(status, res)

server.shutdown()
