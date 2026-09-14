"""
The parts of the booth control panel that are not the window.

Kept separate so they can be tested without a display, and so the mirror logic
has one home rather than being duplicated between the GUI and pusher_hub.py.
"""

from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path

DEFAULT_PORT = 8000
DEFAULTS = {
    "cloud": "https://dhl.roarfun.live",
    "token": "",
    "event": "dhl-the-ring",
    "pin": "1234",
    "port": str(DEFAULT_PORT),
    "mirror": True,
}


def local_ip() -> str:
    """
    The address tablets should type. Opens a UDP socket to a routable address to
    learn which interface would be used — no packet is actually sent, and it
    works with no internet, which matters because the booth often has none.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
        finally:
            sock.close()
    except Exception:
        return "127.0.0.1"


def tablet_url(port: int | str = DEFAULT_PORT, ip: str | None = None) -> str:
    return f"http://{ip or local_ip()}:{port}/tablet"


def load_settings(path: Path) -> dict:
    """Never raises: a corrupt settings file must not stop the booth starting."""
    settings = dict(DEFAULTS)
    try:
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, json.JSONDecodeError, ValueError):
        pass
    return settings


def save_settings(path: Path, settings: dict) -> bool:
    try:
        path.write_text(json.dumps({k: settings.get(k, v) for k, v in DEFAULTS.items()},
                                   indent=2), encoding="utf-8")
        return True
    except OSError:
        return False


def digest(payload) -> str:
    """Stable fingerprint used to avoid re-sending unchanged data."""
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def next_backoff(current: float, floor: float = 4.0, ceiling: float = 60.0) -> float:
    """Doubling backoff with a floor and a ceiling."""
    return min(ceiling, max(floor, current * 2 or floor))


def valid_port(value: str | int) -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_PORT
    return port if 1024 <= port <= 65535 else DEFAULT_PORT
