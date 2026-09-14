"""Runs the JS behaviour test for the wall display via node, if available."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wall_scroll_behaviour():
    result = subprocess.run(
        ["node", str(ROOT / "tests" / "test_wall.js")],
        capture_output=True, text=True, timeout=60)
    combined = result.stdout + result.stderr
    if "Cannot find module 'jsdom'" in combined:
        pytest.skip("jsdom not installed (run: npm install jsdom)")
    assert result.returncode == 0, combined
    assert "0 failed" in result.stdout


def test_hub_serves_wall_locally():
    """The wall page must be served by the hub itself, not fetched from the cloud."""
    import sys, importlib, tempfile, os
    os.environ["HUB_DB"] = tempfile.mktemp(suffix=".db")
    sys.path.insert(0, str(ROOT / "booth"))
    import hub
    importlib.reload(hub)
    from fastapi.testclient import TestClient
    c = TestClient(hub.app)
    for path in ("/wall", "/board"):
        r = c.get(path)
        assert r.status_code == 200
        assert "/api/leaderboard" in r.text        # reads the LOCAL feed
        assert "roarfun.live" not in r.text          # never the cloud


def test_control_panel_wall_button_is_local():
    src = (ROOT / "booth" / "booth_control.py").read_text()
    assert "/wall" in src
    assert 'f"{cloud}/board"' not in src             # old cloud target is gone
