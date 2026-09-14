"""Booth hub tests — registration, search, timing, consent, voiding, audit."""

import importlib
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "booth"))


@pytest.fixture()
def booth(monkeypatch):
    monkeypatch.setenv("HUB_DB", tempfile.mktemp(suffix=".db"))
    monkeypatch.setenv("STAFF_PIN", "4321")
    import hub
    importlib.reload(hub)
    from fastapi.testclient import TestClient
    return TestClient(hub.app)


def register(client, first="David", surname="P", company="Vodafone"):
    return client.post("/api/visitors", json={"first_name": first, "surname": surname,
                                              "company": company, "actor": "rig1"}).json()


# ------------------------------------------------------------------ identity
def test_public_name_is_full_for_private_event(booth):
    person = register(booth)
    assert person["display"] == "DAVID P"
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 60000})
    assert "DAVID P" in booth.get("/api/leaderboard").text


def test_each_visitor_gets_a_stable_unique_id(booth):
    a, b = register(booth), register(booth, "Tomas", "B", "DBAG")
    assert a["id"] != b["id"] and len(a["id"]) == 36


def test_duplicate_registration_is_flagged_not_blocked(booth):
    register(booth)
    again = register(booth)
    assert len(again["possible_duplicates"]) == 1
    assert again["id"]                                   # still created


# ------------------------------------------------------------------ search
def test_search_matches_name_and_company(booth):
    register(booth, "David", "P", "Vodafone")
    register(booth, "Tomas", "B", "Siemens")
    assert len(booth.get("/api/search?q=vodafone").json()["results"]) == 1
    assert len(booth.get("/api/search?q=david").json()["results"]) == 1
    assert len(booth.get("/api/search?q=david vodafone").json()["results"]) == 1
    assert booth.get("/api/search?q=nobody").json()["results"] == []


def test_search_shows_their_current_position(booth):
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 58000})
    hit = booth.get("/api/search?q=vodafone").json()["results"][0]
    assert hit["best_lap"] == "0:58.000" and hit["rank"] == 1
    assert hit["opted_in"] is False


# ------------------------------------------------------------------ timing
def test_only_the_best_lap_counts(booth):
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 58000})
    slower = booth.post("/api/runs", json={"visitor_id": person["id"],
                                           "lap_ms": 62000}).json()
    assert slower["improved"] is False
    assert slower["best_lap"] == "0:58.000"              # keeps the better one
    faster = booth.post("/api/runs", json={"visitor_id": person["id"],
                                           "lap_ms": 54500}).json()
    assert faster["improved"] is True and faster["best_lap"] == "0:54.500"
    assert booth.get("/api/leaderboard").json()["count"] == 1   # one row, not three


@pytest.mark.parametrize("ms,ok", [(19_999, False), (20_000, True),
                                   (900_000, True), (900_001, False)])
def test_implausible_laps_are_rejected(booth, ms, ok):
    person = register(booth)
    status = booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": ms}).status_code
    assert (status == 200) is ok


def test_ranking_is_by_time_ascending(booth):
    for name, ms in [("Anna", 60000), ("Boris", 55000), ("Cyril", 57000)]:
        p = register(booth, name, name[0], "ACME")
        booth.post("/api/runs", json={"visitor_id": p["id"], "lap_ms": ms})
    board = booth.get("/api/leaderboard").json()["leaderboard"]
    assert [e["best_lap_ms"] for e in board] == [55000, 57000, 60000]
    assert [e["rank"] for e in board] == [1, 2, 3]


def test_unknown_visitor_cannot_have_a_lap(booth):
    assert booth.post("/api/runs", json={"visitor_id": "nope", "lap_ms": 60000}).status_code == 404


# ------------------------------------------------------------------ consent
def test_consent_requires_a_valid_international_number(booth):
    person = register(booth)
    for phone, ok in [("+421900123456", True), ("+49 151 2345678", True),
                      ("0900123456", False), ("+421", False), ("hello", False)]:
        status = booth.post("/api/consent", json={
            "visitor_id": person["id"], "phone": phone,
            "channel": "sms", "consent": True}).status_code
        assert (status == 200) is ok, phone


def test_consent_must_be_given(booth):
    person = register(booth)
    assert booth.post("/api/consent", json={
        "visitor_id": person["id"], "phone": "+421900123456",
        "channel": "sms", "consent": False}).status_code == 400


def test_consent_is_exposed_separately_from_the_board(booth):
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 58000})
    booth.post("/api/consent", json={"visitor_id": person["id"],
                                     "phone": "+421900123456",
                                     "channel": "whatsapp", "consent": True})
    assert "900123456" not in booth.get("/api/leaderboard").text
    subs = booth.get("/api/subscribers").json()["subscribers"]
    assert subs[0]["phone"] == "+421900123456" and subs[0]["channel"] == "whatsapp"


def test_reopting_updates_rather_than_duplicates(booth):
    person = register(booth)
    for channel in ("sms", "whatsapp"):
        booth.post("/api/consent", json={"visitor_id": person["id"],
                                         "phone": "+421900123456",
                                         "channel": channel, "consent": True})
    subs = booth.get("/api/subscribers").json()["subscribers"]
    assert len(subs) == 1 and subs[0]["channel"] == "whatsapp"


# ------------------------------------------------------------------ corrections
def test_voiding_a_lap_needs_the_pin_and_restores_the_previous_best(booth):
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 61000})
    mistake = booth.post("/api/runs", json={"visitor_id": person["id"],
                                            "lap_ms": 34000}).json()
    assert booth.get("/api/leaderboard").json()["leaderboard"][0]["best_lap"] == "0:34.000"
    assert booth.post("/api/runs/void", json={"run_id": mistake["run_id"],
                                              "pin": "0000"}).status_code == 403
    assert booth.post("/api/runs/void", json={"run_id": mistake["run_id"],
                                              "pin": "4321"}).status_code == 200
    assert booth.get("/api/leaderboard").json()["leaderboard"][0]["best_lap"] == "1:01.000"


# ------------------------------------------------------------------ records
def test_audit_trail_records_actions_without_storing_the_number(booth):
    import sqlite3, os
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 58000})
    booth.post("/api/consent", json={"visitor_id": person["id"],
                                     "phone": "+421900123456",
                                     "channel": "sms", "consent": True})
    conn = sqlite3.connect(os.environ["HUB_DB"])
    actions = {row[0] for row in conn.execute("SELECT action FROM audit")}
    assert {"visitor.create", "run.add", "consent.add"} <= actions
    leaked = conn.execute(
        "SELECT COUNT(*) FROM audit WHERE detail LIKE '%900123456%'").fetchone()[0]
    assert leaked == 0


def test_csv_export_contains_everything_needed(booth):
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 58000})
    text = booth.get("/api/export.csv").text
    assert "first_name" in text and "David" in text and "0:58.000" in text


def test_stats_and_health(booth):
    person = register(booth)
    booth.post("/api/runs", json={"visitor_id": person["id"], "lap_ms": 58000})
    stats = booth.get("/api/stats").json()
    assert stats["visitors"] == 1 and stats["runs"] == 1 and stats["today"] == 1
    assert booth.get("/healthz").json()["ok"] is True
