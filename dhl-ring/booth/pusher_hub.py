"""
Hub pusher — mirrors the booth outward.

Runs on the booth PC beside the hub, in its own process so a network call can
never stall registration or timing. Reads two things from the hub and sends
them to the cloud:

    /api/leaderboard  ->  POST /ingest            public board, no contact data
    /api/subscribers  ->  POST /sync/subscribers  opt-ins, so the cloud can send

Design rules carried over from the earlier pusher:
  * outbound only — the booth is never reachable from the internet
  * latest-wins retry, not a queue: a stale board is worthless once a newer one
    exists, so we retry the newest state with backoff
  * every exception is logged and swallowed; this process must not die mid-show

Usage:
    python pusher_hub.py                 # reads .env / environment
    python pusher_hub.py --once          # single cycle
    python pusher_hub.py --dry-run       # print, send nothing
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests

LOG = logging.getLogger("hub-pusher")


class Config:
    def __init__(self) -> None:
        self.hub_url = os.getenv("HUB_URL", "http://127.0.0.1:8000").rstrip("/")
        self.cloud_url = os.getenv("CLOUD_URL", "https://orbweaver.roarfun.live").rstrip("/")
        self.ingest_token = os.getenv("INGEST_TOKEN", "")
        self.poll_seconds = float(os.getenv("POLL_SECONDS", "4"))
        self.subscriber_seconds = float(os.getenv("SUBSCRIBER_SECONDS", "30"))
        self.heartbeat_seconds = float(os.getenv("HEARTBEAT_SECONDS", "60"))
        self.request_timeout = float(os.getenv("REQUEST_TIMEOUT", "8"))
        self.max_backoff = float(os.getenv("MAX_BACKOFF_SECONDS", "60"))
        self.all_days = os.getenv("ALL_DAYS", "false").lower() == "true"
        self.log_file = os.getenv("LOG_FILE", "pusher_hub.log")


def setup_logging(path: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    LOG.setLevel(logging.INFO)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    LOG.addHandler(stream)
    try:
        rotating = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        rotating.setFormatter(fmt)
        LOG.addHandler(rotating)
    except OSError as exc:
        LOG.warning("file logging disabled: %s", exc)


def load_dotenv(path: str = ".env") -> None:
    file = Path(path)
    if not file.exists():
        return
    for raw in file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def fetch_board(cfg: Config, session: requests.Session) -> dict:
    url = f"{cfg.hub_url}/api/leaderboard" + ("?all_days=true" if cfg.all_days else "")
    response = session.get(url, timeout=cfg.request_timeout)
    response.raise_for_status()
    return response.json()


def fetch_subscribers(cfg: Config, session: requests.Session) -> list[dict]:
    response = session.get(f"{cfg.hub_url}/api/subscribers", timeout=cfg.request_timeout)
    response.raise_for_status()
    return response.json().get("subscribers", [])


def send(cfg: Config, session: requests.Session, path: str, payload: dict) -> bool:
    try:
        response = session.post(
            f"{cfg.cloud_url}{path}", json=payload,
            headers={"Authorization": f"Bearer {cfg.ingest_token}"},
            timeout=cfg.request_timeout)
    except requests.RequestException as exc:
        LOG.warning("%s failed (network): %s", path, exc)
        return False
    if response.status_code >= 400:
        LOG.warning("%s rejected: HTTP %s %s", path, response.status_code, response.text[:300])
        return False
    return True


def run(cfg: Config, once: bool = False, dry_run: bool = False) -> int:
    session = requests.Session()
    last_board_digest: str | None = None
    last_subs_digest: str | None = None
    last_board_ok = 0.0
    last_subs_check = 0.0
    board_pending = False
    backoff = 0.0
    next_attempt = 0.0

    LOG.info("mirroring %s -> %s every %.1fs", cfg.hub_url, cfg.cloud_url, cfg.poll_seconds)

    while True:
        started = time.monotonic()
        try:
            board = fetch_board(cfg, session)
            digest = hashlib.sha256(
                json.dumps(board["leaderboard"], sort_keys=True).encode()).hexdigest()
            changed = digest != last_board_digest
            if changed:
                LOG.info("board changed: %d entries", board["count"])
                last_board_digest = digest
            if changed or (time.monotonic() - last_board_ok) >= cfg.heartbeat_seconds:
                board_pending = True

            if dry_run:
                print(json.dumps(board, indent=2, ensure_ascii=False))
                print(json.dumps(fetch_subscribers(cfg, session), indent=2))
                return 0

            if board_pending and time.monotonic() >= next_attempt:
                if send(cfg, session, "/ingest", board):
                    LOG.info("pushed %d entries", board["count"])
                    board_pending = False
                    backoff = 0.0
                    last_board_ok = time.monotonic()
                else:
                    backoff = min(cfg.max_backoff, max(cfg.poll_seconds, backoff * 2 or 2))
                    next_attempt = time.monotonic() + backoff
                    LOG.info("retrying newest board in %.0fs", backoff)

            # Opt-ins change far more slowly than lap times, so sync them less often.
            if time.monotonic() - last_subs_check >= cfg.subscriber_seconds:
                last_subs_check = time.monotonic()
                subs = fetch_subscribers(cfg, session)
                subs_digest = hashlib.sha256(
                    json.dumps(subs, sort_keys=True).encode()).hexdigest()
                if subs_digest != last_subs_digest:
                    payload = {"subscribers": [
                        {"visitor_id": s["visitor_id"], "phone": s["phone"],
                         "channel": s["channel"], "consent_at": s.get("consent_at", "")}
                        for s in subs]}
                    if send(cfg, session, "/sync/subscribers", payload):
                        LOG.info("synced %d subscribers", len(subs))
                        last_subs_digest = subs_digest

        except requests.RequestException as exc:
            LOG.error("hub unreachable: %s (is the hub running?)", exc)
        except Exception:
            LOG.exception("unexpected error in cycle")

        if once:
            return 0
        time.sleep(max(0.5, cfg.poll_seconds - (time.monotonic() - started)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Mirror the booth hub to the cloud")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--env", default=".env")
    args = parser.parse_args()

    load_dotenv(args.env)
    cfg = Config()
    setup_logging(cfg.log_file)
    if not args.dry_run and not cfg.ingest_token:
        LOG.error("INGEST_TOKEN is not set — refusing to start")
        return 2
    try:
        return run(cfg, once=args.once, dry_run=args.dry_run)
    except KeyboardInterrupt:
        LOG.info("stopped by operator")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
