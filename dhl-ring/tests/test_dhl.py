"""DHL The Ring specifics: full names, country field, manual sends, backup, fullscreen."""
import importlib, sys, tempfile, os, time
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "booth"))
sys.path.insert(0, str(ROOT / "cloud"))


@pytest.fixture()
def booth(monkeypatch, tmp_path):
    monkeypatch.setenv("HUB_DB", str(tmp_path / "booth.db"))
    monkeypatch.setenv("BACKUP_CSV", str(tmp_path / "backup.csv"))
    import hub; importlib.reload(hub)
    from fastapi.testclient import TestClient
    return hub, TestClient(hub.app)


def test_full_name_shown(booth):
    hub, c = booth
    v = c.post("/api/visitors", json={"first_name": "David", "surname": "Pecl",
                                      "company": "Germany"}).json()
    assert v["display"] == "DAVID PECL"
    c.post("/api/runs", json={"visitor_id": v["id"], "lap_ms": 54000})
    row = c.get("/api/leaderboard").json()["leaderboard"][0]
    assert row["name"] == "DAVID PECL"
    assert row["team"] == "Germany"          # country carried in the team field


def read_backup():
    """Parse the backup CSV into rows, skipping the header."""
    import csv
    with open(os.environ["BACKUP_CSV"], newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_csv_backup_written_per_entry(booth):
    hub, c = booth
    v = c.post("/api/visitors", json={"first_name": "Anna", "surname": "Novak",
                                      "company": "Czechia"}).json()
    c.post("/api/runs", json={"visitor_id": v["id"], "lap_ms": 57000})
    rows = read_backup()
    reg = [r for r in rows if r["kind"] == "register"]
    laps = [r for r in rows if r["kind"] == "lap"]
    assert len(reg) == 1 and reg[0]["first_name"] == "Anna"
    assert reg[0]["surname"] == "Novak" and reg[0]["country"] == "Czechia"
    assert len(laps) == 1 and laps[0]["lap"] == "0:57.000"
    assert laps[0]["visitor_id"] == v["id"]


def test_backup_survives_db_loss(booth):
    """The point of the CSV: entries are recoverable even if the DB vanishes."""
    hub, c = booth
    for i in range(3):
        v = c.post("/api/visitors", json={"first_name": f"P{i}", "surname": "X",
                                          "company": "Poland"}).json()
        c.post("/api/runs", json={"visitor_id": v["id"], "lap_ms": 55000 + i})
    Path(os.environ["HUB_DB"]).unlink()             # simulate total DB loss
    rows = read_backup()
    assert len([r for r in rows if r["kind"] == "register"]) == 3
    assert len([r for r in rows if r["kind"] == "lap"]) == 3
    # every lap is still attributable to a visitor and readable by a human
    assert all(r["visitor_id"] and r["lap"] for r in rows if r["kind"] == "lap")
    assert {r["first_name"] for r in rows if r["kind"] == "register"} == {"P0", "P1", "P2"}


def test_backup_is_append_only_and_ordered(booth):
    """Entries must land in the order they happened — it is the recovery record."""
    hub, c = booth
    ids = []
    for i in range(4):
        v = c.post("/api/visitors", json={"first_name": f"D{i}", "surname": "Y",
                                          "company": "Austria"}).json()
        ids.append(v["id"])
        c.post("/api/runs", json={"visitor_id": v["id"], "lap_ms": 60000 - i * 100})
    rows = read_backup()
    assert [r["kind"] for r in rows] == ["register", "lap"] * 4     # interleaved, in order
    assert [r["first_name"] for r in rows if r["kind"] == "register"] == ["D0","D1","D2","D3"]


def test_backup_records_slower_laps_too(booth):
    """A slower lap does not change the board, but must still be in the record."""
    hub, c = booth
    v = c.post("/api/visitors", json={"first_name": "Eva", "surname": "Z",
                                      "company": "Italy"}).json()
    c.post("/api/runs", json={"visitor_id": v["id"], "lap_ms": 54000})
    c.post("/api/runs", json={"visitor_id": v["id"], "lap_ms": 59000})   # slower
    laps = [r for r in read_backup() if r["kind"] == "lap"]
    assert [l["lap"] for l in laps] == ["0:54.000", "0:59.000"]
    # board still shows only the best
    assert c.get("/api/leaderboard").json()["leaderboard"][0]["best_lap"] == "0:54.000"


@pytest.fixture()
def cloud(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "t")
    import main; importlib.reload(main)
    import notify; notify.engine = notify.Engine()
    from fastapi.testclient import TestClient
    return main, notify, TestClient(main.app)


AUTH = {"Authorization": "Bearer t"}


def _board(main, client, n=3):
    rows = [{"id": f"d{i}", "rank": i+1, "name": f"DRIVER {i}", "team": "Germany",
             "best_lap_ms": 54000+i*500, "best_lap": f"0:{54+i}.000"} for i in range(n)]
    client.post("/ingest", json={"schema_version": 2, "event": "dhl-the-ring",
                                 "count": n, "leaderboard": rows}, headers=AUTH)
    return rows


def test_ingest_does_not_auto_send(cloud):
    main, notify, c = cloud
    _board(main, c)
    c.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "d0", "phone": "+421900111222", "channel": "sms"}]})
    # a new faster time arrives — must NOT trigger any message on its own
    r = c.post("/ingest", headers=AUTH, json={"schema_version": 2, "event": "dhl-the-ring",
        "count": 1, "leaderboard": [{"id": "x", "rank": 1, "name": "FAST",
        "best_lap_ms": 50000, "best_lap": "0:50.000"}]})
    assert r.json()["notifications_sent"] == 0
    assert len(notify.engine.log) == 0             # nothing sent automatically


def test_manual_position_broadcast(cloud):
    main, notify, c = cloud
    _board(main, c)
    c.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "d0", "phone": "+421900111222", "channel": "sms"},
        {"visitor_id": "d1", "phone": "+421900333444", "channel": "sms"}]})
    r = c.post("/admin/broadcast", headers=AUTH, json={"kind": "position"})
    assert r.json()["sent"] == 2
    bodies = [m["body"] for m in notify.engine.recent()]
    assert any("You're now #1 of 3 at DHL The Ring" in b for b in bodies)


def test_broadcast_cooldown(cloud):
    main, notify, c = cloud
    _board(main, c)
    c.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "d0", "phone": "+421900111222", "channel": "sms"}]})
    assert c.post("/admin/broadcast", headers=AUTH, json={"kind": "position"}).json()["sent"] == 1
    second = c.post("/admin/broadcast", headers=AUTH, json={"kind": "position"}).json()
    assert second["sent"] == 0 and second["skipped"] == "cooldown"


def test_broadcast_respects_optout(cloud):
    main, notify, c = cloud
    _board(main, c)
    c.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "d0", "phone": "+421900111222", "channel": "sms"}]})
    # opt out via stop token
    token = notify.engine.subscribers["d0"].token
    c.post("/api/stop", json={"token": token})
    r = c.post("/admin/broadcast", headers=AUTH, json={"kind": "position"})
    assert r.json()["sent"] == 0                   # nobody left to message


def test_summary_broadcast_wording(cloud):
    main, notify, c = cloud
    _board(main, c)
    c.post("/sync/subscribers", headers=AUTH, json={"subscribers": [
        {"visitor_id": "d0", "phone": "+421900111222", "channel": "sms"}]})
    c.post("/admin/broadcast", headers=AUTH, json={"kind": "summary"})
    assert any("You finished #1 of 3 at DHL The Ring" in m["body"]
               for m in notify.engine.recent())


def test_wall_has_fullscreen_and_dhl_theme():
    wall = (ROOT / "booth" / "wall.html").read_text()
    assert 'id="fs"' in wall                        # fullscreen button
    assert "requestFullscreen" in wall
    assert "DHL The Ring" in wall
    assert "#FFCC00" in wall                         # DHL yellow


# ------------------------------------------------------------- tablet & search
def test_returning_visitor_found_by_name(booth):
    """Lookup is by name + surname now that company is gone."""
    hub, c = booth
    c.post("/api/visitors", json={"first_name": "Thomas", "surname": "Bohunek",
                                  "company": "Germany"})
    c.post("/api/visitors", json={"first_name": "Anna", "surname": "Klein",
                                  "company": "Austria"})
    assert len(c.get("/api/search?q=bohunek").json()["results"]) == 1
    assert len(c.get("/api/search?q=thomas bohunek").json()["results"]) == 1
    assert len(c.get("/api/search?q=klein").json()["results"]) == 1
    assert c.get("/api/search?q=nobody").json()["results"] == []


def test_search_still_matches_country(booth):
    hub, c = booth
    c.post("/api/visitors", json={"first_name": "Jan", "surname": "Novak",
                                  "company": "Czechia"})
    assert len(c.get("/api/search?q=czechia").json()["results"]) == 1


def test_tablet_form_has_country_dropdown_and_full_surname():
    tablet = (ROOT / "booth" / "tablet.html").read_text()
    assert '<select id="co"></select>' in tablet          # country is a dropdown
    assert "Surname initial" not in tablet                # full surname now
    # the surname input itself must no longer be length-capped (the time fields
    # legitimately still use maxlength, so scope the check to that one input)
    sur_line = next(l for l in tablet.splitlines() if 'id="sur"' in l)
    assert "maxlength" not in sur_line, sur_line
    assert "COUNTRIES" in tablet and '"Germany"' in tablet
    assert "Full name is shown" in tablet


def test_country_list_is_english_and_deduplicated():
    tablet = (ROOT / "booth" / "tablet.html").read_text()
    start = tablet.index("const COUNTRIES = [")
    block = tablet[start:tablet.index("];", start)]
    names = [n.strip().strip('"') for n in block.split("[")[1].split(",")]
    names = [n for n in names if n and n != "Other"]
    assert len(names) == len(set(names)), "duplicate country in the list"
    assert "Germany" in names and "United States" in names
    assert all(n[0].isupper() for n in names)


def test_hub_serves_wall_and_assets_locally(booth):
    """Wall + background must work with no internet."""
    hub, c = booth
    r = c.get("/wall")
    assert r.status_code == 200
    assert "/api/leaderboard" in r.text          # reads the local feed
    assert "roarfun.live" not in r.text          # no cloud dependency
    assert "/assets/background.jpg" in r.text    # background served locally
