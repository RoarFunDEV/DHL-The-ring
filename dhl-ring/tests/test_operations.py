"""
Tests for the operations layer: metrics collection, the dashboard endpoint, and
the parts of the booth control panel that can be checked without a screen.
"""

import importlib
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud"))
sys.path.insert(0, str(ROOT / "booth"))


# --------------------------------------------------------------------- metrics
@pytest.fixture()
def m():
    import metrics
    return metrics.Metrics()


def test_requests_are_counted_and_bucketed(m):
    for _ in range(5):
        m.record_request("/v1/leaderboard.json", 200, 3.0, 6000)
    snap = m.snapshot()
    assert snap["requests_last_hour"] == 5
    assert snap["top_paths"]["/v1/leaderboard.json"] == 5
    assert snap["status_counts"]["2xx"] == 5
    assert snap["bytes_last_hour"] == 30000


def test_revalidation_rate_is_the_share_answered_empty(m):
    for _ in range(9):
        m.record_request("/v1/leaderboard.json", 304, 1.0)
    m.record_request("/v1/leaderboard.json", 200, 3.0, 6000)
    assert m.snapshot()["revalidation_rate"] == 0.9


def test_revalidation_rate_is_none_before_any_feed_traffic(m):
    m.record_request("/board", 200, 2.0)
    assert m.snapshot()["revalidation_rate"] is None


def test_errors_are_recorded_with_context(m):
    m.record_request("/ingest", 500, 12.0)
    snap = m.snapshot()
    assert snap["errors_last_hour"] == 1
    assert snap["recent_errors"][0]["path"] == "/ingest"
    assert snap["status_counts"]["5xx"] == 1


def test_client_errors_are_not_counted_as_failures(m):
    m.record_request("/ingest", 401, 1.0)
    snap = m.snapshot()
    assert snap["errors_last_hour"] == 0          # 4xx is the caller's problem
    assert snap["status_counts"]["4xx"] == 1


def test_ingest_freshness(m):
    assert m.snapshot()["last_ingest_age_sec"] is None
    m.record_ingest(57)
    snap = m.snapshot()
    assert snap["last_ingest_count"] == 57
    assert snap["ingest_total"] == 1
    assert snap["last_ingest_age_sec"] < 1


def test_message_outcomes(m):
    m.record_message(True)
    m.record_message(True)
    m.record_message(False)
    snap = m.snapshot()
    assert snap["messages_sent"] == 2 and snap["messages_failed"] == 1


def test_latency_percentiles(m):
    for ms in range(1, 101):
        m.record_request("/x", 200, float(ms))
    snap = m.snapshot()
    assert 45 <= snap["latency_p50_ms"] <= 55
    assert snap["latency_p95_ms"] >= 90


def test_series_is_gap_filled_for_charting(m):
    m.record_request("/x", 200, 1.0)
    series = m.snapshot()["series"]
    assert len(series) == 60                      # a full hour, no holes
    assert series[-1]["requests"] == 1
    assert all(set(p) >= {"requests", "errors", "messages"} for p in series)


def test_memory_is_bounded(m):
    """A four-day show must not grow this structure without limit."""
    import metrics
    for i in range(5000):
        m.record_request(f"/p{i % 10}", 200, 1.0)
    assert len(m.buckets) <= metrics.WINDOW_MINUTES
    assert len(m.latencies_ms) <= 500
    assert len(m.recent_errors) <= 25
    assert len(m.by_path) == 10


# ------------------------------------------------------------------ dashboard
@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("INGEST_TOKEN", "test-token")
    import main
    importlib.reload(main)
    from fastapi.testclient import TestClient
    return TestClient(main.app)


AUTH = {"Authorization": "Bearer test-token"}


def test_dashboard_requires_the_token(client):
    assert client.get("/admin/metrics").status_code == 401
    assert client.get("/admin/metrics", headers=AUTH).status_code == 200


def test_dashboard_reports_live_state(client):
    payload = {"schema_version": 2, "event": "electronica-2026", "count": 1,
               "leaderboard": [{"id": "a", "rank": 1, "name": "D. P.",
                                "best_lap_ms": 55206, "best_lap": "0:55.206"}]}
    client.post("/ingest", json=payload, headers=AUTH)
    client.get("/v1/leaderboard.json")
    snap = client.get("/admin/metrics", headers=AUTH).json()
    assert snap["board"]["count"] == 1
    assert snap["board"]["leader"] == "0:55.206"
    assert snap["last_ingest_age_sec"] is not None
    assert snap["store"] in ("memory", "redis")
    assert isinstance(snap["dry_run"], bool)


def test_middleware_records_every_request(client):
    before = client.get("/admin/metrics", headers=AUTH).json()["requests_last_hour"]
    client.get("/board")
    client.get("/monitor")
    after = client.get("/admin/metrics", headers=AUTH).json()["requests_last_hour"]
    assert after > before


def test_metrics_never_break_a_response(client, monkeypatch):
    """A metrics failure must not take the feed down with it."""
    import metrics
    monkeypatch.setattr(metrics.metrics, "record_request",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert client.get("/v1/leaderboard.json").status_code == 200


# --------------------------------------------------------- booth control panel
def test_control_panel_imports_without_a_display():
    """Catches syntax and import errors even where tkinter cannot open a window."""
    import ast
    source = (ROOT / "booth" / "booth_control.py").read_text()
    tree = ast.parse(source)
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in ("start", "stop", "toggle", "open_tablet", "open_display",
                     "export", "test_cloud", "_mirror_loop", "on_close"):
        assert required in names, required


def test_control_panel_module_actually_imports():
    """With tkinter present this catches real breakage, not just syntax."""
    tk = pytest.importorskip("tkinter")
    import booth_control
    assert hasattr(booth_control, "Control")
    assert callable(booth_control.local_ip)


def test_control_panel_finds_a_usable_address():
    import control_core
    ip = control_core.local_ip()
    assert ip.count(".") == 3 and all(p.isdigit() for p in ip.split("."))
    assert control_core.tablet_url(8000, ip) == f"http://{ip}:8000/tablet"


def test_settings_round_trip(tmp_path):
    """An operator should type the token once, not every morning."""
    import control_core
    target = tmp_path / "booth_settings.json"
    control_core.save_settings(target, {"cloud": "https://x.roarfun.live", "token": "abc",
                                        "event": "e", "pin": "9999", "port": "8100",
                                        "mirror": False})
    loaded = control_core.load_settings(target)
    assert loaded["token"] == "abc" and loaded["port"] == "8100"
    assert loaded["mirror"] is False


def test_corrupt_settings_do_not_stop_the_booth(tmp_path):
    import control_core
    target = tmp_path / "booth_settings.json"
    target.write_text("{not json at all", encoding="utf-8")
    loaded = control_core.load_settings(target)
    assert loaded == control_core.DEFAULTS          # falls back, never raises


def test_missing_settings_file_gives_defaults(tmp_path):
    import control_core
    assert control_core.load_settings(tmp_path / "nope.json") == control_core.DEFAULTS


@pytest.mark.parametrize("value,expected", [
    ("8000", 8000), (9100, 9100), ("", 8000), ("abc", 8000),
    ("80", 8000), ("99999", 8000), (None, 8000)])
def test_port_validation(value, expected):
    """A typo in the port box must not make the booth unstartable."""
    import control_core
    assert control_core.valid_port(value) == expected


def test_backoff_doubles_within_bounds():
    import control_core
    steps, current = [], 0.0
    for _ in range(8):
        current = control_core.next_backoff(current)
        steps.append(current)
    assert steps[:4] == [4.0, 8.0, 16.0, 32.0]
    assert max(steps) == 60.0                      # capped, never runaway


def test_digest_detects_change_and_ignores_ordering():
    import control_core
    a = [{"id": "x", "rank": 1}, {"id": "y", "rank": 2}]
    b = [{"rank": 1, "id": "x"}, {"rank": 2, "id": "y"}]
    assert control_core.digest(a) == control_core.digest(b)
    assert control_core.digest(a) != control_core.digest(a + [{"id": "z", "rank": 3}])


def test_control_panel_does_not_shell_out():
    """No terminals, no subprocesses — everything runs in-process by design."""
    source = (ROOT / "booth" / "booth_control.py").read_text()
    for forbidden in ("subprocess", "os.system", "popen"):
        assert forbidden not in source.lower(), forbidden


# ------------------------------------------------------- real window smoke test
def test_control_panel_starts_the_booth_for_real(tmp_path, monkeypatch):
    """
    Builds the actual window off-screen, starts the booth from the button's code
    path, and checks a tablet could really talk to it. Skipped where there is no
    display, but it runs in CI under xvfb and catches layout and threading
    errors that a syntax check never would.
    """
    pytest.importorskip("tkinter")
    import os
    import requests
    if not os.environ.get("DISPLAY"):
        pytest.skip("no display available")

    import booth_control
    monkeypatch.setattr(booth_control, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setenv("HUB_DB", str(tmp_path / "smoke.db"))
    # hub reads its database path at import time and earlier tests already
    # imported it, so force a reload against this test's fresh database.
    import hub
    importlib.reload(hub)

    app = booth_control.Control()
    try:
        app.update()
        app.var_port.set("8711")
        app.var_mirror.set(False)          # offline: no cloud needed for this test
        app.start()
        time.sleep(4)

        base = "http://127.0.0.1:8711"
        assert app.var_hub_state.get() == "Running"
        assert app.var_tablet.get().endswith("/tablet")
        assert requests.get(f"{base}/healthz", timeout=5).json()["ok"] is True
        assert requests.get(f"{base}/tablet", timeout=5).status_code == 200

        visitor = requests.post(f"{base}/api/visitors", timeout=5, json={
            "first_name": "Anna", "surname": "K", "company": "Siemens"}).json()
        requests.post(f"{base}/api/runs", timeout=5,
                      json={"visitor_id": visitor["id"], "lap_ms": 57000})
        assert requests.get(f"{base}/api/leaderboard", timeout=5).json()["count"] == 1

        app._tick()
        app.update()
        assert "1 visitors registered" in app.var_stats.get()

        app.stop()
        time.sleep(1)
        assert app.var_hub_state.get() == "Stopped"
    finally:
        app.destroy()
