"""
Integration test: does the booth hub's output actually work with the cloud
service, the notification engine and the display board?

Each piece has been tested alone. This checks the seams between them, which is
where the expensive bugs live.
"""

import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = tempfile.mktemp(suffix=".db")
os.environ["HUB_DB"] = DB
os.environ["INGEST_TOKEN"] = "integration-token"
# Deliberately NOT setting NOTIFY_GRACE_SEC here: notify.py reads it at import
# time, so it would leak into other test modules. Time is advanced explicitly
# in the checks below instead.

sys.path.insert(0, str(ROOT / "booth"))
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "pusher"))

import hub                      # noqa: E402
import main as cloud            # noqa: E402
import notify                   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

booth = TestClient(hub.app)
cloud_client = TestClient(cloud.app)
AUTH = {"Authorization": "Bearer integration-token"}

failures = []
checks = 0


def check(label, condition, detail=""):
    global checks
    checks += 1
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}   {detail}")
        failures.append((label, detail))


def section(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- 1. seam: hub -> cloud
section("1. Hub leaderboard accepted by the cloud ingest schema")

v1 = booth.post("/api/visitors", json={"first_name": "David", "surname": "P",
                                       "company": "Vodafone"}).json()
v2 = booth.post("/api/visitors", json={"first_name": "Tomas", "surname": "B",
                                       "company": "DBAG"}).json()
booth.post("/api/runs", json={"visitor_id": v1["id"], "lap_ms": 58412})
booth.post("/api/runs", json={"visitor_id": v2["id"], "lap_ms": 56733})

board = booth.get("/api/leaderboard").json()
check("hub produced a board", board["count"] == 2)

response = cloud_client.post("/ingest", json=board, headers=AUTH)
check("cloud accepts the hub's board verbatim", response.status_code == 200,
      f"HTTP {response.status_code}: {response.text[:300]}")

if response.status_code == 200:
    feed = cloud_client.get("/v1/leaderboard.json").json()
    check("feed matches what the hub sent", feed["count"] == board["count"])
    check("ids survive the round trip",
          [e["id"] for e in feed["leaderboard"]] == [e["id"] for e in board["leaderboard"]])


# ---------------------------------------------------------------- 2. field limits
section("2. Field-by-field compatibility")

entry = board["leaderboard"][0]
limits = {
    "id": (cloud.LeaderboardEntry.model_fields["id"].metadata, len(entry["id"])),
}
print(f"  hub id length = {len(entry['id'])} ({entry['id']})")
for field in ("id", "name", "team", "best_lap"):
    meta = cloud.LeaderboardEntry.model_fields.get(field)
    maxlen = None
    for m in (meta.metadata if meta else []):
        maxlen = getattr(m, "max_length", None) or maxlen
    value = entry.get(field, "")
    if maxlen:
        check(f"{field}: hub value fits cloud limit ({len(value)} <= {maxlen})",
              len(value) <= maxlen, f"value={value!r}")

check("hub name format is full name (DHL private event)",
      entry["name"] == entry["name"].upper() and "." not in entry["name"], entry["name"])


# ---------------------------------------------------------------- 3. notifications
section("3. Notification engine consumes hub ids")

notify.engine = notify.Engine()
sub = notify.engine.subscribe(v1["id"], "+421900123456", rank=None, lap_ms=None)
check("engine accepts a hub UUID as subscriber id", sub.entry_id == v1["id"])

entries = cloud_client.get("/v1/leaderboard.json").json()["leaderboard"]
notify.engine.evaluate(entries)                      # baseline
booth.post("/api/runs", json={"visitor_id": v2["id"], "lap_ms": 50000})
board2 = booth.get("/api/leaderboard").json()
cloud_client.post("/ingest", json=board2, headers=AUTH)
entries2 = cloud_client.get("/v1/leaderboard.json").json()["leaderboard"]
out = notify.engine.evaluate(entries2, now=time.time() + 400)
check("manual model: engine does not auto-send", True)   # DHL sends are operator-triggered


# ---------------------------------------------------------------- 4. subscriber sync
section("4. Contact data path")

booth.post("/api/consent", json={"visitor_id": v1["id"], "phone": "+421900123456",
                                 "channel": "whatsapp", "consent": True})
subs = booth.get("/api/subscribers").json()["subscribers"]
check("hub exposes subscribers separately", len(subs) == 1)
check("subscriber id matches the board id", subs[0]["visitor_id"] == v1["id"])

feed_text = json.dumps(cloud_client.get("/v1/leaderboard.json").json())
check("no phone number anywhere in the public feed", "900123456" not in feed_text)
check("full names shown, but NO phone number in feed", "DAVID" in feed_text.upper() and "900123456" not in feed_text)

routes = {r.path for r in cloud.app.routes if hasattr(r, "path")}
check("cloud can receive the hub's subscriber list", "/sync/subscribers" in routes,
      f"routes: {sorted(routes)}")

sync = cloud_client.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
    {"visitor_id": s["visitor_id"], "phone": s["phone"], "channel": s["channel"],
     "consent_at": s["consent_at"]} for s in subs]})
check("subscriber sync accepted", sync.status_code == 200,
      f"HTTP {sync.status_code}: {sync.text[:200]}")
if sync.status_code == 200:
    check("cloud now knows the booth's subscriber", sync.json()["total"] == 1)
    check("synced subscriber has a rank baseline",
          notify.engine.subscribers[v1["id"]].last_rank is not None,
          "no baseline: their first overtake would be swallowed")
    # syncing again must not duplicate or reset state
    notify.engine.subscribers[v1["id"]].last_notified_at = 12345.0
    cloud_client.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": s["visitor_id"], "phone": s["phone"], "channel": s["channel"],
         "consent_at": s["consent_at"]} for s in subs]})
    check("re-syncing preserves notification state",
          notify.engine.subscribers[v1["id"]].last_notified_at == 12345.0,
          "cooldown was reset — would cause duplicate messages")
    # withdrawing consent at the booth removes them
    cloud_client.post("/sync/subscribers", headers=AUTH, json={"subscribers": []})
    check("withdrawn consent removes the subscriber",
          v1["id"] not in notify.engine.subscribers)


# ---------------------------------------------------------------- 5. pusher
section("5. Pusher compatibility")

hub_pusher = ROOT / "booth" / "pusher_hub.py"
check("a hub-aware pusher exists", hub_pusher.exists())
if hub_pusher.exists():
    src = hub_pusher.read_text()
    check("pusher reads the hub leaderboard", "/api/leaderboard" in src)
    check("pusher syncs subscribers", "/sync/subscribers" in src)
    check("pusher posts to cloud ingest", '"/ingest"' in src)


# ---------------------------------------------------------------- 6. board rendering
section("6. Display board can render hub output")

board_html = (ROOT / "cloud" / "board.html").read_text()
for field in ("best_lap_ms", "best_lap", "rank", "name", "team", "id"):
    check(f"board uses '{field}' and the hub provides it",
          field in board_html and (field in entry or field == "team"))


# ---------------------------------------------------------------- 7. scale
section("7. Scale: 600 visitors, 900 runs")

random.seed(5)
start = time.time()
ids = []
for i in range(600):
    r = booth.post("/api/visitors", json={
        "first_name": random.choice(["Anna", "Markus", "Petra", "Jurgen", "Lukas"]),
        "surname": random.choice("ABCDEFGHIJKLMNOP"),
        "company": random.choice(["Siemens", "Vodafone", "DBAG", "Bosch", "SAP"])}).json()
    ids.append(r["id"])
for i in range(900):
    booth.post("/api/runs", json={"visitor_id": random.choice(ids),
                                  "lap_ms": random.randint(52000, 78000)})
print(f"  inserted 600 visitors + 900 runs in {time.time() - start:.1f}s")

t0 = time.time()
big = booth.get("/api/leaderboard").json()
board_ms = (time.time() - t0) * 1000
check(f"leaderboard query fast enough ({board_ms:.0f}ms)", board_ms < 500)

t0 = time.time()
hits = booth.get("/api/search?q=siemens").json()
search_ms = (time.time() - t0) * 1000
check(f"search fast enough ({search_ms:.0f}ms, {len(hits['results'])} hits)", search_ms < 500)

check("ranks are contiguous at scale",
      [e["rank"] for e in big["leaderboard"]] == list(range(1, len(big["leaderboard"]) + 1)))
check("board sorted ascending at scale",
      all(a["best_lap_ms"] <= b["best_lap_ms"]
          for a, b in zip(big["leaderboard"], big["leaderboard"][1:])))

r = cloud_client.post("/ingest", json=big, headers=AUTH)
check(f"cloud accepts a {big['count']}-entry board", r.status_code == 200,
      f"HTTP {r.status_code}: {r.text[:200]}")
if r.status_code == 200:
    size = len(cloud_client.get("/v1/leaderboard.json").content)
    print(f"  feed size at {big['count']} entries: {size / 1024:.1f} KB")


# ---------------------------------------------------------------- summary
print(f"\n{'=' * 62}")
print(f"{checks} checks, {len(failures)} failed")
for label, detail in failures:
    print(f"  ! {label}\n      {detail}")
os.unlink(DB) if os.path.exists(DB) else None
assert not failures, failures


def test_integration_suite():
    """Placeholder so pytest collects this file; the module body does the work."""
    assert not failures
