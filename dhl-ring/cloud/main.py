"""
Orbweaver live leaderboard — cloud service (Railway).

    POST /ingest                 bearer-authenticated, booth pusher writes here
    GET  /v1/leaderboard.json    public, cached, THE URL handed to Orbweaver
    GET  /healthz                liveness + freshness for monitoring

State is latest-snapshot-only. In-memory by default; set REDIS_URL to survive
redeploys and restarts. No contact data ever touches this service — the feed
carries id / name / team / lap only.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from pydantic import BaseModel, Field

import metrics as metrics_mod
import notify

SCHEMA_VERSION = 2
STATE_KEY = "leaderboard:latest"

INGEST_TOKEN = os.getenv("INGEST_TOKEN", "")
PUBLIC_API_KEY = os.getenv("PUBLIC_API_KEY", "")  # optional; empty = open endpoint
EVENT_ID = os.getenv("EVENT_ID", "dhl-the-ring")
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "5"))
ALLOWED_ORIGINS = [o.strip() for o in os.getenv("ALLOWED_ORIGINS", "*").split(",") if o.strip()]
REDIS_URL = os.getenv("REDIS_URL", "")
STALE_AFTER = int(os.getenv("STALE_AFTER_SECONDS", "180"))
ENABLE_TEST_TOOLS = os.getenv("ENABLE_TEST_TOOLS", "true").lower() == "true"


# --------------------------------------------------------------------------- models
class LeaderboardEntry(BaseModel):
    id: str = Field(min_length=1, max_length=64)   # UUIDs are 36 chars
    rank: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=80)
    team: str | None = Field(default=None, max_length=80)
    best_lap_ms: int = Field(ge=1, le=3_600_000)
    best_lap: str = Field(min_length=1, max_length=20)


class StopRequest(BaseModel):
    token: str = Field(min_length=4, max_length=64)


class IdRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)


class OptInRequest(BaseModel):
    id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=6, max_length=20)
    consent: bool
    channel: str = Field(default="sms", pattern="^(sms|whatsapp)$")


class IngestPayload(BaseModel):
    schema_version: int = Field(ge=1, le=SCHEMA_VERSION)
    event: str = Field(min_length=1, max_length=64)
    generated_at: str | None = None
    count: int = Field(ge=0)
    leaderboard: list[LeaderboardEntry] = Field(max_length=5000)


# --------------------------------------------------------------------------- storage
class MemoryStore:
    def __init__(self) -> None:
        self._doc: dict | None = None

    async def get(self) -> dict | None:
        return self._doc

    async def set(self, doc: dict) -> None:
        self._doc = doc


class RedisStore:
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis  # imported only when REDIS_URL is set

        self._client = redis.from_url(url, decode_responses=True)

    async def get(self) -> dict | None:
        raw = await self._client.get(STATE_KEY)
        return json.loads(raw) if raw else None

    async def set(self, doc: dict) -> None:
        await self._client.set(STATE_KEY, json.dumps(doc, ensure_ascii=False))


store: MemoryStore | RedisStore = MemoryStore()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    if REDIS_URL:
        try:
            store = RedisStore(REDIS_URL)
        except Exception as exc:  # never fail to boot over an optional cache
            print(f"[warn] Redis unavailable ({exc}); falling back to in-memory state")
    yield


app = FastAPI(title="Orbweaver Live Leaderboard", version="2.0", lifespan=lifespan)
# Leaderboard JSON is highly repetitive text and compresses to roughly a fifth.
app.add_middleware(GZipMiddleware, minimum_size=500)


@app.middleware("http")
async def record_metrics(request, call_next):
    """Times every request for the dashboard. Never allowed to break a response."""
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        metrics_mod.metrics.record_request(request.url.path, 500,
                                           (time.perf_counter() - started) * 1000)
        raise
    try:
        size = int(response.headers.get("content-length", 0) or 0)
        metrics_mod.metrics.record_request(
            request.url.path, response.status_code,
            (time.perf_counter() - started) * 1000, size)
    except Exception:
        pass
    return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "OPTIONS"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- auth
def require_ingest_token(authorization: str = Header(default="")) -> None:
    if not INGEST_TOKEN:
        raise HTTPException(status_code=503, detail="ingest not configured")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="invalid ingest token")


def check_public_key(x_api_key: str = Header(default="")) -> None:
    """Optional. Only enforced if PUBLIC_API_KEY is set — lets us revoke or
    track access without forcing a key on Orbweaver."""
    if PUBLIC_API_KEY and not secrets.compare_digest(x_api_key, PUBLIC_API_KEY):
        raise HTTPException(status_code=401, detail="invalid api key")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def empty_document() -> dict:
    """Served before the first push so the client page renders instead of erroring."""
    return {
        "schema_version": SCHEMA_VERSION,
        "event": EVENT_ID,
        "updated_at": None,
        "count": 0,
        "leaderboard": [],
    }


# --------------------------------------------------------------------------- routes
@app.post("/ingest", dependencies=[Depends(require_ingest_token)])
async def ingest(payload: IngestPayload) -> dict:
    entries = [e.model_dump(exclude_none=True) for e in payload.leaderboard]
    document = {
        "schema_version": SCHEMA_VERSION,
        "event": payload.event,
        # Server clock, not the booth clock: the booth PC's time cannot be trusted
        # and this value drives the client's "live vs stale" indicator.
        "updated_at": now_iso(),
        "count": len(entries),
        "leaderboard": entries,
    }
    await store.set({"document": document, "generated_at": payload.generated_at})
    metrics_mod.metrics.record_ingest(len(entries))

    # DHL event: notifications are operator-triggered only. We do NOT auto-send on
    # ingest — the board just updates. Manual broadcasts go via /admin/broadcast.
    return {"ok": True, "stored": len(entries), "updated_at": document["updated_at"],
            "notifications_sent": 0}


@app.get("/v1/leaderboard.json", dependencies=[Depends(check_public_key)])
async def leaderboard(if_none_match: str = Header(default="")) -> Response:
    state = await store.get()
    document = state["document"] if state else empty_document()
    body = json.dumps(document, ensure_ascii=False)
    etag = '"' + hashlib.sha256(body.encode("utf-8")).hexdigest()[:16] + '"'
    headers = {
        "Cache-Control": f"public, max-age={CACHE_TTL}, stale-while-revalidate=30",
        "ETag": etag,
    }
    # Clients poll far more often than the data actually changes. When their
    # copy is still current, answer with an empty 304 instead of resending the
    # whole board — on a busy show that is the difference between megabytes and
    # kilobytes of egress.
    if if_none_match and etag in [tag.strip() for tag in if_none_match.split(",")]:
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type="application/json", headers=headers)


SAMPLE_FILE = Path(__file__).with_name("sample_leaderboard.json")


@app.get("/v1/sample-leaderboard.json")
async def sample_leaderboard() -> Response:
    """
    A fixed, committed leaderboard that never changes with live data. Hand this
    URL to a client for a stable preview while the real board is being tested,
    then switch them to /v1/leaderboard.json for the event. Edit the file
    sample_leaderboard.json in the repo to change what it shows.
    """
    if not SAMPLE_FILE.exists():
        raise HTTPException(status_code=404, detail="no sample file committed")
    body = SAMPLE_FILE.read_text(encoding="utf-8")
    return Response(content=body, media_type="application/json",
                    headers={"Cache-Control": "public, max-age=60"})


@app.get("/healthz")
async def healthz() -> dict:
    state = await store.get()
    if not state:
        return {"ok": True, "has_data": False, "age_seconds": None, "count": 0}
    updated_at = state["document"]["updated_at"]
    age = (
        datetime.now(timezone.utc) - datetime.strptime(updated_at, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    ).total_seconds()
    return {
        "ok": age < STALE_AFTER,
        "has_data": True,
        "age_seconds": round(age, 1),
        "count": state["document"]["count"],
        "booth_generated_at": state.get("generated_at"),
    }


@app.get("/optin", response_class=HTMLResponse)
async def optin_page() -> HTMLResponse:
    """Page a driver lands on after scanning the QR code carrying their entry id."""
    page = Path(__file__).with_name("optin.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="opt-in page not deployed")
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.post("/api/optin")
async def api_optin(request: OptInRequest) -> dict:
    if not request.consent:
        raise HTTPException(status_code=400, detail="Consent is required")
    phone = request.phone.strip().replace(" ", "")
    if not notify.valid_phone(phone):
        raise HTTPException(status_code=400,
                            detail="Enter the number in full international form, e.g. +421900123456")

    state = await store.get()
    board = state["document"]["leaderboard"] if state else []
    entry = next((e for e in board if e["id"] == request.id), None)

    sub = notify.engine.subscribe(
        request.id, phone,
        rank=entry["rank"] if entry else None,
        lap_ms=entry["best_lap_ms"] if entry else None,
        channel=request.channel,
    )
    await notify.engine.send(
        phone,
        notify.welcome_text(entry["name"] if entry else "driver",
                            entry["rank"] if entry else None,
                            len(board) if entry else None,
                            sub),
        "welcome",
        sub.channel,
    )
    return {"ok": True, "dry_run": notify.DRY_RUN, "channel": sub.channel}


@app.get("/stop", response_class=HTMLResponse)
async def stop_page() -> HTMLResponse:
    """
    Opt-out landing page. An alphanumeric sender ID cannot receive replies, so
    this link — not "reply STOP" — is the working opt-out for EU recipients.
    """
    page = Path(__file__).with_name("stop.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="stop page not deployed")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.post("/api/stop")
async def api_stop(request: StopRequest) -> dict:
    # Deliberately a POST behind a button: link scanners and preview bots follow
    # URLs in messages, and a GET would let them unsubscribe people silently.
    if not notify.engine.unsubscribe_by_token(request.token):
        raise HTTPException(status_code=404, detail="This link is no longer valid")
    return {"ok": True}


class SubscriberIn(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=6, max_length=20)
    channel: str = Field(default="sms", pattern="^(sms|whatsapp)$")
    consent_at: str = ""


class SubscriberSync(BaseModel):
    subscribers: list[SubscriberIn] = Field(max_length=5000)


@app.post("/sync/subscribers", dependencies=[Depends(require_ingest_token)])
async def sync_subscribers(payload: SubscriberSync) -> dict:
    """
    The booth owns opt-ins; this mirrors them outward so the cloud can send.
    Existing subscribers keep their notification state, so a sync never causes
    a duplicate message or resets somebody's cooldown.
    """
    state = await store.get()
    board = state["document"]["leaderboard"] if state else []
    ranks = {e["id"]: e for e in board}

    incoming = {s.visitor_id: s for s in payload.subscribers}
    added = 0
    welcomed: list = []
    for visitor_id, sub in incoming.items():
        existing = notify.engine.subscribers.get(visitor_id)
        if existing:
            existing.phone = sub.phone
            existing.channel = sub.channel
            continue
        entry = ranks.get(visitor_id)
        new_sub = notify.engine.subscribe(
            visitor_id, sub.phone,
            rank=entry["rank"] if entry else None,
            lap_ms=entry["best_lap_ms"] if entry else None,
            channel=sub.channel,
        )
        added += 1
        # Welcome the driver who just opted in on the tablet. Only fires for a
        # genuinely NEW subscriber — a re-sync of the same list hits the
        # "existing" branch above, so nobody is ever welcomed twice.
        welcomed.append((new_sub, entry))

    # Someone removed at the booth (withdrawn consent) stops receiving messages.
    removed = [k for k in notify.engine.subscribers if k not in incoming]
    for key in removed:
        notify.engine.unsubscribe(key)

    # Send the welcomes after the registry is settled. A send failure must never
    # make the booth's sync look failed — the opt-in itself is already recorded.
    sent = 0
    for new_sub, entry in welcomed:
        try:
            await notify.engine.send(
                new_sub.phone,
                notify.welcome_text(entry["name"] if entry else "driver",
                                    entry["rank"] if entry else None,
                                    len(board) if entry else None,
                                    new_sub),
                "welcome", new_sub.channel)
            sent += 1
        except Exception as exc:                       # noqa: BLE001
            print(f"[warn] welcome SMS failed for {new_sub.entry_id}: {exc}")

    return {"ok": True, "total": len(notify.engine.subscribers), "welcomed": sent,
            "added": added, "removed": len(removed)}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    """Operator console for the notification layer. Asks for the token itself."""
    page = Path(__file__).with_name("admin.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="admin page not deployed")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


class BroadcastRequest(BaseModel):
    kind: str = Field(default="position", pattern="^(position|summary)$")


@app.post("/admin/broadcast", dependencies=[Depends(require_ingest_token)])
async def admin_broadcast(request: BroadcastRequest) -> dict:
    """Operator-triggered SMS to all opted-in drivers. 30s cooldown, opt-out honoured."""
    state = await store.get()
    board = state["document"]["leaderboard"] if state else []
    result = await notify.engine.broadcast(board, request.kind)
    return result


@app.get("/admin/metrics", dependencies=[Depends(require_ingest_token)])
async def admin_metrics() -> dict:
    state = await store.get()
    document = state["document"] if state else None
    snapshot = metrics_mod.metrics.snapshot()
    snapshot["board"] = {
        "count": document["count"] if document else 0,
        "event": document["event"] if document else EVENT_ID,
        "updated_at": document["updated_at"] if document else None,
        "leader": document["leaderboard"][0]["best_lap"]
                  if document and document["leaderboard"] else None,
    }
    snapshot["subscribers"] = len(notify.engine.subscribers)
    snapshot["store"] = "redis" if REDIS_URL else "memory"
    snapshot["dry_run"] = notify.DRY_RUN
    return snapshot


@app.get("/admin/notifications", dependencies=[Depends(require_ingest_token)])
async def notifications_log() -> dict:
    """Everything sent or simulated, newest first. Uses the booth token as auth."""
    return {
        "config": notify.config(),
        "test_tools": ENABLE_TEST_TOOLS,
        "subscribers": notify.engine.subscriber_summary(),
        "messages": notify.engine.recent(),
    }


@app.post("/admin/unsubscribe", dependencies=[Depends(require_ingest_token)])
async def admin_unsubscribe(request: IdRequest) -> dict:
    return {"ok": notify.engine.unsubscribe(request.id)}


@app.post("/admin/simulate-overtake", dependencies=[Depends(require_ingest_token)])
async def simulate_overtake(request: IdRequest) -> dict:
    """
    Testing aid: drop a synthetic faster driver onto the board so a real
    overtake can be demonstrated without touching the booth. Disable with
    ENABLE_TEST_TOOLS=false before a live event.
    """
    if not ENABLE_TEST_TOOLS:
        raise HTTPException(status_code=403, detail="test tools are disabled")
    state = await store.get()
    if not state:
        raise HTTPException(status_code=400, detail="no leaderboard yet")

    board = [dict(e) for e in state["document"]["leaderboard"]]
    target = next((e for e in board if e["id"] == request.id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="driver not on the board")

    faster = max(1000, target["best_lap_ms"] - 500)
    minutes, rest = divmod(faster, 60_000)
    seconds, millis = divmod(rest, 1_000)
    board.append({
        "id": f"sim{len(board):03d}",
        "rank": 0,
        "name": "Z. TESTER",
        "team": "SIMULATED",
        "best_lap_ms": faster,
        "best_lap": f"{minutes}:{seconds:02d}.{millis:03d}",
    })
    board.sort(key=lambda e: e["best_lap_ms"])
    for i, entry in enumerate(board, start=1):
        entry["rank"] = i

    document = {
        "schema_version": SCHEMA_VERSION,
        "event": state["document"]["event"],
        "updated_at": now_iso(),
        "count": len(board),
        "leaderboard": board,
    }
    await store.set({"document": document, "generated_at": state.get("generated_at")})
    sent = await notify.engine.process(board)
    new_rank = next(e["rank"] for e in board if e["id"] == request.id)
    return {"ok": True, "new_rank": new_rank, "notifications_sent": sent}


@app.get("/monitor", response_class=HTMLResponse)
async def monitor() -> HTMLResponse:
    """Self-refreshing timing view. Same origin as the feed, so no CORS involved."""
    page = Path(__file__).with_name("monitor.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="monitor page not deployed")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=300"})


# Optional static assets (e.g. a background image for the board).
_assets = Path(__file__).with_name("assets")
if _assets.is_dir():
    app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")


@app.get("/board", response_class=HTMLResponse)
async def board() -> HTMLResponse:
    """Public display board in RoarFun styling. Reads the same feed, client-side."""
    page = Path(__file__).with_name("board.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="board page not deployed")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=300"})


@app.get("/")
async def root() -> HTMLResponse:
    """
    The public leaderboard, served at the bare domain so the URL on a QR code is
    as short as possible. Phone first: everyone listed, normal scrolling, search.
    The service signpost that used to live here moved to /info.
    """
    page = Path(__file__).with_name("public.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="public page not deployed")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "public, max-age=60"})


@app.get("/info")
async def info() -> dict:
    return {
        "service": "dhl-the-ring-leaderboard",
        "public": "/",
        "feed": "/v1/leaderboard.json",
        "wall": "/board",
        "monitor": "/monitor",
        "admin": "/admin",
    }
