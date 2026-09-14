"""
Booth hub — the source of truth at the stand.

Runs on the main booth PC. Tablets talk to it over the booth's own WiFi; the
wall display is driven from the same machine. Nothing here needs the internet:
if the venue uplink dies, registration, timing and the wall board carry on. The
pusher (separate process) mirrors state outward when a connection exists.

Data model
    visitors   one row per person, stable UUID, never deleted by the app
    runs       every lap entered, including slower ones and voided ones
    consent    phone + channel + timestamp, one row per visitor
    audit      every create, edit and void, with who and when

Best-time-only is a *view* over runs, not a destructive write: a slower lap is
recorded but does not change their board position. That keeps an audit trail
for a prize and means a mistyped fast lap can be voided without losing history.
"""

from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import csv as _csv
import uuid
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

DB_PATH = os.getenv("HUB_DB", str(Path(__file__).with_name("booth.db")))
BACKUP_CSV = os.getenv("BACKUP_CSV", str(Path(__file__).with_name("entries_backup.csv")))
EVENT_ID = os.getenv("EVENT_ID", "dhl-the-ring")
STAFF_PIN = os.getenv("STAFF_PIN", "1234")          # confirms edits and voids
MAX_LAP_MS = 15 * 60 * 1000
MIN_LAP_MS = 20 * 1000

PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS visitors (
    id          TEXT PRIMARY KEY,
    first_name  TEXT NOT NULL,
    surname     TEXT NOT NULL,          -- initial only, e.g. "B."
    company     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    search_key  TEXT NOT NULL           -- lowercased haystack for fast lookup
);
CREATE INDEX IF NOT EXISTS idx_visitors_search ON visitors(search_key);

CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    visitor_id  TEXT NOT NULL REFERENCES visitors(id),
    lap_ms      INTEGER NOT NULL,
    day         TEXT NOT NULL,          -- YYYY-MM-DD, for the daily reset
    entered_at  TEXT NOT NULL,
    entered_by  TEXT NOT NULL DEFAULT '',
    voided      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_runs_visitor ON runs(visitor_id);
CREATE INDEX IF NOT EXISTS idx_runs_day ON runs(day, voided);

CREATE TABLE IF NOT EXISTS consent (
    visitor_id  TEXT PRIMARY KEY REFERENCES visitors(id),
    phone       TEXT NOT NULL,
    channel     TEXT NOT NULL,          -- sms | whatsapp
    consent_at  TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    action      TEXT NOT NULL,
    visitor_id  TEXT,
    detail      TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT ''
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL keeps reads working while a write is in flight, and survives a power
    # cut far better than the default journal — the booth PC will lose power.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def today() -> str:
    return date.today().isoformat()


def fmt(ms: int) -> str:
    minutes, rest = divmod(ms, 60_000)
    seconds, millis = divmod(rest, 1_000)
    return f"{minutes}:{seconds:02d}.{millis:03d}"


def log(conn, action: str, visitor_id: str | None = None,
        detail: str = "", actor: str = "") -> None:
    conn.execute(
        "INSERT INTO audit (at, action, visitor_id, detail, actor) VALUES (?,?,?,?,?)",
        (now_iso(), action, visitor_id, detail, actor),
    )


def backup_row(kind: str, fields: dict) -> None:
    """
    Append one line to a plain CSV on disk immediately after each entry. This is
    a physical, human-readable safety net independent of the database — if the DB
    is ever lost or corrupted, every registration and lap is still here in order.
    A failure to write the backup must never block the actual operation.
    """
    path = os.getenv("BACKUP_CSV", BACKUP_CSV)
    try:
        new_file = not Path(path).exists()
        with open(path, "a", newline="", encoding="utf-8") as fh:
            writer = _csv.writer(fh)
            if new_file:
                writer.writerow(["at", "kind", "visitor_id", "first_name",
                                 "surname", "country", "lap", "detail"])
            writer.writerow([
                now_iso(), kind,
                fields.get("visitor_id", ""), fields.get("first_name", ""),
                fields.get("surname", ""), fields.get("country", ""),
                fields.get("lap", ""), fields.get("detail", ""),
            ])
    except OSError:
        pass


# --------------------------------------------------------------------------- models
class VisitorIn(BaseModel):
    first_name: str = Field(min_length=1, max_length=60)
    surname: str = Field(min_length=1, max_length=20)
    company: str = Field(default="", max_length=80)
    actor: str = Field(default="", max_length=40)


class RunIn(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=64)
    lap_ms: int = Field(ge=MIN_LAP_MS, le=MAX_LAP_MS)
    actor: str = Field(default="", max_length=40)


class ConsentIn(BaseModel):
    visitor_id: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=6, max_length=20)
    channel: str = Field(default="sms", pattern="^(sms|whatsapp)$")
    consent: bool


class VoidIn(BaseModel):
    run_id: str
    pin: str
    actor: str = Field(default="", max_length=40)


# --------------------------------------------------------------------------- helpers
def public_name(first: str, surname: str) -> str:
    """Private event: full name shown in full. 'David' + 'Pecl' -> 'DAVID PECL'."""
    full = f"{first.strip()} {surname.strip()}".strip()
    return full.upper()


def best_runs(conn, day: str | None) -> list[dict]:
    """Best non-voided lap per visitor, ranked. day=None means all days."""
    where = "WHERE r.voided = 0"
    args: list = []
    if day:
        where += " AND r.day = ?"
        args.append(day)
    rows = conn.execute(f"""
        SELECT v.id, v.first_name, v.surname, v.company, MIN(r.lap_ms) AS lap_ms
        FROM runs r JOIN visitors v ON v.id = r.visitor_id
        {where}
        GROUP BY v.id
        ORDER BY lap_ms ASC, MIN(r.entered_at) ASC
    """, args).fetchall()
    out = []
    for rank, row in enumerate(rows, start=1):
        entry = {
            "id": row["id"],
            "rank": rank,
            "name": public_name(row["first_name"], row["surname"]),
            "best_lap_ms": row["lap_ms"],
            "best_lap": fmt(row["lap_ms"]),
        }
        if row["company"]:
            entry["team"] = row["company"]
        out.append(entry)
    return out


# --------------------------------------------------------------------------- app
app = FastAPI(title="RoarFun Booth Hub", version="1.0")
init_db()

# Serve an optional background image (and any assets) locally, so the wall
# display's DHL background works with no internet.
_assets = Path(__file__).with_name("assets")
_assets.mkdir(exist_ok=True)
app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")


@app.get("/wall", response_class=HTMLResponse)
@app.get("/board", response_class=HTMLResponse)
async def wall_page() -> HTMLResponse:
    """The wall-display leaderboard, served locally so it works with no internet."""
    page = Path(__file__).with_name("wall.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="wall page missing")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/tablet", response_class=HTMLResponse)
async def tablet_page() -> HTMLResponse:
    page = Path(__file__).with_name("tablet.html")
    if not page.exists():
        raise HTTPException(status_code=404, detail="tablet page missing")
    return HTMLResponse(page.read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


@app.get("/api/search")
async def search(q: str = Query(default="", max_length=80),
                 day: str | None = None) -> dict:
    """
    Find a returning visitor. The only defence against duplicates, so it matches
    loosely: every word in the query must appear somewhere in name or company.
    """
    terms = [t for t in re.split(r"\s+", q.strip().lower()) if t]
    with closing(connect()) as conn:
        if not terms:
            rows = conn.execute(
                "SELECT * FROM visitors ORDER BY created_at DESC LIMIT 15").fetchall()
        else:
            clause = " AND ".join(["search_key LIKE ?"] * len(terms))
            rows = conn.execute(
                f"SELECT * FROM visitors WHERE {clause} ORDER BY created_at DESC LIMIT 25",
                [f"%{t}%" for t in terms]).fetchall()

        ranked = {e["id"]: e for e in best_runs(conn, day or today())}
        results = []
        for row in rows:
            entry = ranked.get(row["id"])
            has_consent = conn.execute(
                "SELECT 1 FROM consent WHERE visitor_id=? AND revoked=0",
                (row["id"],)).fetchone() is not None
            results.append({
                "id": row["id"],
                "first_name": row["first_name"],
                "surname": row["surname"],
                "company": row["company"],
                "display": public_name(row["first_name"], row["surname"]),
                "best_lap": entry["best_lap"] if entry else None,
                "best_lap_ms": entry["best_lap_ms"] if entry else None,
                "rank": entry["rank"] if entry else None,
                "opted_in": has_consent,
            })
    return {"results": results}


@app.post("/api/visitors")
async def create_visitor(payload: VisitorIn) -> dict:
    visitor_id = str(uuid.uuid4())
    key = f"{payload.first_name} {payload.surname} {payload.company}".lower()
    with closing(connect()) as conn:
        # Warn, don't block: two real people can share a name at a big fair.
        dupes = conn.execute(
            "SELECT id, first_name, surname, company FROM visitors "
            "WHERE lower(first_name)=? AND lower(surname)=? AND lower(company)=?",
            (payload.first_name.lower(), payload.surname.lower(),
             payload.company.lower())).fetchall()
        conn.execute(
            "INSERT INTO visitors (id, first_name, surname, company, created_at, search_key)"
            " VALUES (?,?,?,?,?,?)",
            (visitor_id, payload.first_name.strip(), payload.surname.strip().upper(),
             payload.company.strip(), now_iso(), key))
        log(conn, "visitor.create", visitor_id,
            f"{payload.first_name} {payload.surname} / {payload.company}", payload.actor)
        conn.commit()
    backup_row("register", {"visitor_id": visitor_id, "first_name": payload.first_name,
                            "surname": payload.surname, "country": payload.company})
    return {
        "id": visitor_id,
        "display": public_name(payload.first_name, payload.surname),
        "possible_duplicates": [dict(d) for d in dupes],
    }


@app.post("/api/runs")
async def add_run(payload: RunIn) -> dict:
    with closing(connect()) as conn:
        visitor = conn.execute("SELECT * FROM visitors WHERE id=?",
                               (payload.visitor_id,)).fetchone()
        if visitor is None:
            raise HTTPException(status_code=404, detail="visitor not found")

        previous = conn.execute(
            "SELECT MIN(lap_ms) AS best FROM runs WHERE visitor_id=? AND voided=0 AND day=?",
            (payload.visitor_id, today())).fetchone()["best"]

        run_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runs (id, visitor_id, lap_ms, day, entered_at, entered_by)"
            " VALUES (?,?,?,?,?,?)",
            (run_id, payload.visitor_id, payload.lap_ms, today(), now_iso(), payload.actor))
        log(conn, "run.add", payload.visitor_id, f"{fmt(payload.lap_ms)}", payload.actor)
        conn.commit()

        board = best_runs(conn, today())
        entry = next((e for e in board if e["id"] == payload.visitor_id), None)

    backup_row("lap", {"visitor_id": payload.visitor_id, "lap": fmt(payload.lap_ms)})
    improved = previous is None or payload.lap_ms < previous
    return {
        "run_id": run_id,
        "improved": improved,
        "previous_best": fmt(previous) if previous else None,
        "best_lap": entry["best_lap"] if entry else fmt(payload.lap_ms),
        "rank": entry["rank"] if entry else None,
        "total": len(board),
    }


@app.post("/api/consent")
async def add_consent(payload: ConsentIn) -> dict:
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Consent is required")
    phone = payload.phone.strip().replace(" ", "")
    if not PHONE_RE.match(phone):
        raise HTTPException(status_code=400,
                            detail="Enter the number with its country code, e.g. +49…")
    with closing(connect()) as conn:
        if conn.execute("SELECT 1 FROM visitors WHERE id=?",
                        (payload.visitor_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail="visitor not found")
        conn.execute(
            "INSERT INTO consent (visitor_id, phone, channel, consent_at, revoked)"
            " VALUES (?,?,?,?,0)"
            " ON CONFLICT(visitor_id) DO UPDATE SET phone=excluded.phone,"
            " channel=excluded.channel, consent_at=excluded.consent_at, revoked=0",
            (payload.visitor_id, phone, payload.channel, now_iso()))
        # Never log the number itself.
        log(conn, "consent.add", payload.visitor_id, payload.channel)
        conn.commit()
    return {"ok": True, "channel": payload.channel}


@app.post("/api/runs/void")
async def void_run(payload: VoidIn) -> dict:
    if payload.pin != STAFF_PIN:
        raise HTTPException(status_code=403, detail="Wrong PIN")
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (payload.run_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="run not found")
        conn.execute("UPDATE runs SET voided=1 WHERE id=?", (payload.run_id,))
        log(conn, "run.void", row["visitor_id"], fmt(row["lap_ms"]), payload.actor)
        conn.commit()
    return {"ok": True}


@app.get("/api/leaderboard")
async def leaderboard(day: str | None = None, all_days: bool = False) -> dict:
    with closing(connect()) as conn:
        board = best_runs(conn, None if all_days else (day or today()))
    return {
        "schema_version": 2,
        "event": EVENT_ID,
        "day": None if all_days else (day or today()),
        "updated_at": now_iso(),
        "count": len(board),
        "leaderboard": board,
    }


@app.get("/api/subscribers")
async def subscribers(x_hub_key: str = Header(default="")) -> dict:
    """
    Contact data for the pusher to sync outward. Deliberately a separate
    endpoint from the leaderboard so contact details can never leak into the
    public feed by accident.
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT c.visitor_id, c.phone, c.channel, c.consent_at"
            " FROM consent c WHERE c.revoked = 0").fetchall()
    return {"subscribers": [dict(r) for r in rows]}


@app.get("/api/export.csv")
async def export_csv() -> StreamingResponse:
    with closing(connect()) as conn:
        rows = conn.execute("""
            SELECT v.id, v.first_name, v.surname, v.company, v.created_at,
                   MIN(r.lap_ms) AS best_ms, COUNT(r.id) AS runs,
                   c.phone, c.channel, c.consent_at
            FROM visitors v
            LEFT JOIN runs r ON r.visitor_id = v.id AND r.voided = 0
            LEFT JOIN consent c ON c.visitor_id = v.id AND c.revoked = 0
            GROUP BY v.id ORDER BY best_ms IS NULL, best_ms
        """).fetchall()
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "first_name", "surname", "company", "registered",
                     "best_lap", "runs", "phone", "channel", "consent_at"])
    for r in rows:
        writer.writerow([r["id"], r["first_name"], r["surname"], r["company"],
                         r["created_at"], fmt(r["best_ms"]) if r["best_ms"] else "",
                         r["runs"], r["phone"] or "", r["channel"] or "",
                         r["consent_at"] or ""])
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="orbweaver_visitors.csv"'})


@app.get("/api/stats")
async def stats() -> dict:
    with closing(connect()) as conn:
        visitors = conn.execute("SELECT COUNT(*) c FROM visitors").fetchone()["c"]
        runs = conn.execute("SELECT COUNT(*) c FROM runs WHERE voided=0").fetchone()["c"]
        opted = conn.execute(
            "SELECT COUNT(*) c FROM consent WHERE revoked=0").fetchone()["c"]
        today_board = best_runs(conn, today())
    return {"visitors": visitors, "runs": runs, "opted_in": opted,
            "today": len(today_board), "day": today()}


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True, "db": DB_PATH, "day": today()}
