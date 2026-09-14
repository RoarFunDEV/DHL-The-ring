"""Cloud service endpoint tests — feed, auth, caching, pages, subscriber sync."""

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud"))


def board(*rows):
    """rows = (id, rank, name, ms)"""
    out = []
    for entry_id, rank, name, ms in rows:
        m, r = divmod(ms, 60_000)
        s, ml = divmod(r, 1_000)
        out.append({"id": entry_id, "rank": rank, "name": name, "team": "DBAG",
                    "best_lap_ms": ms, "best_lap": f"{m}:{s:02d}.{ml:03d}"})
    return out


def payload(*rows):
    entries = board(*rows)
    return {"schema_version": 2, "event": "electronica-2026",
            "count": len(entries), "leaderboard": entries}


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "test-token")
    import main as cloud
    importlib.reload(cloud)
    from fastapi.testclient import TestClient
    return TestClient(cloud.app)


AUTH = {"Authorization": "Bearer test-token"}
SAMPLE = ("aaa", 1, "D. P.", 55206), ("bbb", 2, "T. B.", 56733)


# ------------------------------------------------------------------ the feed
def test_empty_feed_before_first_push(client):
    body = client.get("/v1/leaderboard.json").json()
    assert body["count"] == 0 and body["leaderboard"] == [] and body["updated_at"] is None


def test_ingest_then_serve(client):
    assert client.post("/ingest", json=payload(*SAMPLE), headers=AUTH).status_code == 200
    body = client.get("/v1/leaderboard.json").json()
    assert body["count"] == 2 and body["leaderboard"][0]["name"] == "D. P."
    assert body["updated_at"] is not None


def test_ingest_requires_a_valid_token(client):
    assert client.post("/ingest", json=payload(*SAMPLE)).status_code == 401
    assert client.post("/ingest", json=payload(*SAMPLE),
                       headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_uuid_length_ids_are_accepted(client):
    """The booth hub mints 36-character UUIDs — this used to be rejected at 32."""
    long_id = "cac2597e-57d8-4ada-b449-b6848839d7a2"
    body = payload(*SAMPLE)
    body["leaderboard"][0]["id"] = long_id
    assert client.post("/ingest", json=body, headers=AUTH).status_code == 200
    assert client.get("/v1/leaderboard.json").json()["leaderboard"][0]["id"] == long_id


def test_malformed_payload_rejected(client):
    bad = payload(*SAMPLE)
    bad["leaderboard"][0]["best_lap_ms"] = -5
    assert client.post("/ingest", json=bad, headers=AUTH).status_code == 422


# ------------------------------------------------------------------ efficiency
def test_etag_revalidation(client):
    client.post("/ingest", json=payload(*SAMPLE), headers=AUTH)
    first = client.get("/v1/leaderboard.json")
    etag = first.headers["etag"]
    again = client.get("/v1/leaderboard.json", headers={"If-None-Match": etag})
    assert again.status_code == 304 and not again.content

    client.post("/ingest", json=payload(("aaa", 1, "D. P.", 50000)), headers=AUTH)
    changed = client.get("/v1/leaderboard.json", headers={"If-None-Match": etag})
    assert changed.status_code == 200


def test_feed_is_cacheable(client):
    assert "max-age" in client.get("/v1/leaderboard.json").headers["cache-control"]


# ------------------------------------------------------------------ pages
@pytest.mark.parametrize("path", ["/", "/board", "/monitor", "/admin", "/optin", "/stop",
                                  "/healthz"])
def test_pages_render(client, path):
    assert client.get(path).status_code == 200


def test_admin_data_requires_the_token(client):
    assert client.get("/admin/notifications").status_code == 401
    assert client.get("/admin/notifications", headers=AUTH).status_code == 200


# ------------------------------------------------------------------ subscribers
def test_subscriber_sync_round_trip(client):
    client.post("/ingest", json=payload(*SAMPLE), headers=AUTH)
    subs = {"subscribers": [{"visitor_id": "aaa", "phone": "+421900123456",
                             "channel": "whatsapp", "consent_at": "2026-11-10T09:00:00Z"}]}
    result = client.post("/sync/subscribers", json=subs, headers=AUTH)
    assert result.status_code == 200 and result.json()["total"] == 1

    listing = client.get("/admin/notifications", headers=AUTH).json()
    assert listing["subscribers"][0]["id"] == "aaa"
    assert "123456" not in listing["subscribers"][0]["phone"]      # masked
    assert listing["subscribers"][0]["rank"] == 1                  # baseline seeded

    # removing them at the booth removes them here
    assert client.post("/sync/subscribers", json={"subscribers": []},
                       headers=AUTH).json()["total"] == 0


def test_subscriber_sync_requires_the_token(client):
    assert client.post("/sync/subscribers", json={"subscribers": []}).status_code == 401


def test_contact_data_never_enters_the_public_feed(client):
    client.post("/ingest", json=payload(*SAMPLE), headers=AUTH)
    client.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "aaa", "phone": "+421900123456", "channel": "sms"}]})
    assert "900123456" not in client.get("/v1/leaderboard.json").text


# ------------------------------------------------------------------ opt-out
def test_sample_endpoint_is_static_and_independent(client):
    """The committed sample must not change when live data arrives."""
    before = client.get("/v1/sample-leaderboard.json")
    assert before.status_code == 200
    sample_count = before.json()["count"]

    client.post("/ingest", json=payload(*SAMPLE), headers=AUTH)   # live data changes
    after = client.get("/v1/sample-leaderboard.json").json()
    assert after["count"] == sample_count                         # sample unchanged
    assert client.get("/v1/leaderboard.json").json()["count"] == 2   # live differs


def test_stop_link_unsubscribes_once(client):
    import notify
    client.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "aaa", "phone": "+421900123456", "channel": "sms"}]})
    token = notify.engine.subscribers["aaa"].token
    assert client.post("/api/stop", json={"token": "wrong"}).status_code == 404
    assert client.post("/api/stop", json={"token": token}).status_code == 200
    assert client.post("/api/stop", json={"token": token}).status_code == 404
