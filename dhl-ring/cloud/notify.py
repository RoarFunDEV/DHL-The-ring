"""
SMS re-engagement — MVP.

Two things the client asked to see proven:
  1. an opt-in confirmation ("thanks, we'll let you know if your rank changes")
  2. a real overtake alert ("someone has passed you")

Everything runs in DRY-RUN unless Twilio credentials are present, so the whole
flow can be exercised — and demonstrated — without an account or any spend.
Messages are recorded either way and readable at /admin/notifications.

The hard part here is not sending; it is deciding *when not to* send. At a booth
cycling hundreds of drivers, ranks churn constantly and naive alerts read as
spam. The suppression rules below are most of the value.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
import time
from dataclasses import dataclass, field

import httpx

import metrics as metrics_mod

# --------------------------------------------------------------------------- config
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
# A Messaging Service is a pool of senders. Set this and Twilio picks the right
# one per destination: the ROARFUN alphanumeric ID where it is supported, a
# phone number where it is not (the US and Canada, among others). With an
# international audience that routing is the whole point.
TWILIO_SERVICE_SID = os.getenv("TWILIO_MESSAGING_SERVICE_SID", "")
# WhatsApp goes through the same Messages API; only the sender and recipient
# carry a "whatsapp:" prefix. In the Twilio Sandbox this is the shared test
# number; in production it is your own approved WhatsApp sender.
TWILIO_WHATSAPP_FROM = os.getenv("TWILIO_WHATSAPP_FROM", "")
DRY_RUN = not (TWILIO_SID and TWILIO_TOKEN
               and (TWILIO_FROM or TWILIO_SERVICE_SID or TWILIO_WHATSAPP_FROM))

# Suppression rules — every one of these exists to stop a real failure mode.
COOLDOWN_SEC = int(os.getenv("NOTIFY_COOLDOWN_SEC", "600"))    # max 1 msg per person per 10 min
GRACE_SEC = int(os.getenv("NOTIFY_GRACE_SEC", "10"))          # silence while they are still driving
GAP_SEC = int(os.getenv("NOTIFY_GAP_SEC", "300"))              # after an outage, rebaseline, don't replay
MIN_DROP = int(os.getenv("NOTIFY_MIN_DROP", "1"))              # places lost before it is worth a message
TOP_N = int(os.getenv("NOTIFY_TOP_N", "0"))                    # 0 = alert everyone; 10 = only the top ten

PHONE_RE = re.compile(r"^\+[1-9]\d{7,14}$")                    # E.164, e.g. +421900123456

# Public base address, used to build the opt-out link carried in every message.
# An alphanumeric sender ID cannot receive replies, so "reply STOP" is not an
# option in Europe — the link is the only working opt-out.
PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")


def mask(phone: str) -> str:
    """Never show a full number in a console someone might screenshot."""
    return phone[:-4] + "\u2022\u2022\u2022\u2022" if len(phone) > 4 else phone


@dataclass
class Subscriber:
    entry_id: str
    phone: str
    consent_at: float
    token: str = ""                   # unguessable opt-out key, not the public entry id
    channel: str = "sms"              # "sms" or "whatsapp"
    last_rank: int | None = None      # rank at the last notification decision
    last_notified_at: float = 0.0
    last_lap_ms: int | None = None
    last_lap_at: float = 0.0


@dataclass
class SentMessage:
    at: float
    to: str
    body: str
    kind: str
    delivered: bool
    detail: str = ""
    channel: str = "sms"


@dataclass
class Engine:
    subscribers: dict[str, Subscriber] = field(default_factory=dict)
    log: list[SentMessage] = field(default_factory=list)
    last_ingest_at: float = 0.0

    # ---------------------------------------------------------------- opt-in
    def subscribe(self, entry_id: str, phone: str, now: float | None = None,
                  rank: int | None = None, lap_ms: int | None = None,
                  channel: str = "sms") -> Subscriber:
        """
        Seed the baseline from where the driver stands at the moment they opt in.
        Without this, the first evaluation after signup is spent establishing a
        baseline — so a driver who opts in and is immediately passed would hear
        nothing about the one overtake they most cared about.
        """
        now = now or time.time()
        sub = Subscriber(
            entry_id=entry_id, phone=phone, consent_at=now,
            token=secrets.token_urlsafe(8),
            channel="whatsapp" if channel == "whatsapp" else "sms",
            last_rank=rank, last_lap_ms=lap_ms,
            last_lap_at=now if lap_ms is not None else 0.0,
        )
        self.subscribers[entry_id] = sub
        return sub

    def unsubscribe(self, entry_id: str) -> bool:
        return self.subscribers.pop(entry_id, None) is not None

    def find_by_token(self, token: str) -> Subscriber | None:
        if not token:
            return None
        # Constant-time compare so the endpoint cannot be used to guess tokens.
        for sub in self.subscribers.values():
            if sub.token and secrets.compare_digest(sub.token, token):
                return sub
        return None

    def unsubscribe_by_token(self, token: str) -> bool:
        sub = self.find_by_token(token)
        return self.unsubscribe(sub.entry_id) if sub else False

    # ---------------------------------------------------------------- deciding
    def evaluate(self, board: list[dict], now: float | None = None) -> list[tuple[Subscriber, str]]:
        """
        Compare this board against what each subscriber was last told, and return
        the messages that should actually go out.
        """
        now = now or time.time()
        gap = self.last_ingest_at and (now - self.last_ingest_at) > GAP_SEC
        self.last_ingest_at = now

        by_id = {e["id"]: e for e in board}
        total = len(board)
        outgoing: list[tuple[Subscriber, str]] = []

        for sub in self.subscribers.values():
            entry = by_id.get(sub.entry_id)
            if entry is None:
                continue
            rank = entry["rank"]

            # Track when this driver last improved, so we can stay quiet while
            # they are obviously still standing at the rig.
            if entry["best_lap_ms"] != sub.last_lap_ms:
                sub.last_lap_ms = entry["best_lap_ms"]
                sub.last_lap_at = now

            if sub.last_rank is None:          # first sighting: set a baseline only
                sub.last_rank = rank
                continue

            dropped = rank - sub.last_rank      # positive means they lost places

            # --- suppression, in order of how often each one saves you ---
            if gap:                                    # uplink came back: don't replay history
                sub.last_rank = rank
                continue
            if dropped < MIN_DROP:                     # improved or unchanged
                sub.last_rank = rank
                continue
            if now - sub.last_lap_at < GRACE_SEC:      # still driving; they can see the screen
                continue
            if now - sub.last_notified_at < COOLDOWN_SEC:
                continue
            if TOP_N and sub.last_rank > TOP_N:        # only care about the sharp end
                sub.last_rank = rank
                continue

            body = (
                f"Your position changed: now #{rank} of {total}"
                f" (was #{sub.last_rank}). Best lap {entry['best_lap']}."
                f"{opt_out(sub)}"
            )
            outgoing.append((sub, body))
            sub.last_rank = rank
            sub.last_notified_at = now

        return outgoing

    # ---------------------------------------------------------------- sending
    async def send(self, to: str, body: str, kind: str, channel: str = "sms") -> SentMessage:
        if DRY_RUN:
            msg = SentMessage(time.time(), to, body, kind, True,
                              "dry-run (no provider configured)", channel)
            self.log.append(msg)
            metrics_mod.metrics.record_message(True)
            return msg
        auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
        url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                if channel == "whatsapp":
                    sender = TWILIO_WHATSAPP_FROM
                    if not sender.startswith("whatsapp:"):
                        sender = "whatsapp:" + sender
                    data = {"To": "whatsapp:" + to, "Body": body, "From": sender}
                else:
                    data = {"To": to, "Body": body}
                    if TWILIO_SERVICE_SID:
                        data["MessagingServiceSid"] = TWILIO_SERVICE_SID
                    else:
                        data["From"] = TWILIO_FROM
                response = await client.post(
                    url, headers={"Authorization": f"Basic {auth}"}, data=data,
                )
            ok = response.status_code < 300
            detail = "sent" if ok else f"HTTP {response.status_code}: {response.text[:200]}"
        except Exception as exc:                 # a failed SMS must never break ingest
            ok, detail = False, f"error: {exc}"
        msg = SentMessage(time.time(), to, body, kind, ok, detail, channel)
        self.log.append(msg)
        metrics_mod.metrics.record_message(ok)
        return msg

    async def process(self, board: list[dict]) -> int:
        outgoing = self.evaluate(board)
        if outgoing:
            await asyncio.gather(
                *(self.send(s.phone, b, "overtake", s.channel) for s, b in outgoing))
        return len(outgoing)

    # ---------------------------------------------------------------- manual
    manual_last_at: float = 0.0
    MANUAL_COOLDOWN = 30      # seconds between manual broadcasts

    def manual_cooldown_left(self, now: float | None = None) -> int:
        now = now or time.time()
        return max(0, int(self.MANUAL_COOLDOWN - (now - self.manual_last_at)))

    async def broadcast(self, board: list[dict], kind: str,
                        now: float | None = None) -> dict:
        """
        Operator-triggered send. kind='position' texts each opted-in driver their
        current standing; kind='summary' sends the end-of-event line. Respects
        opt-out (revoked subscribers are gone from the registry) and a short
        global cooldown so a double-press can't double-send.
        """
        now = now or time.time()
        left = self.manual_cooldown_left(now)
        if left > 0:
            return {"sent": 0, "cooldown_left": left, "skipped": "cooldown"}

        by_id = {e["id"]: e for e in board}
        jobs = []
        for sub in self.subscribers.values():
            entry = by_id.get(sub.entry_id)
            if entry is None:
                continue
            if kind == "summary":
                body = summary_text(entry["rank"], len(board), entry["best_lap"], sub)
            else:
                body = position_text(entry["rank"], len(board), entry["best_lap"], sub)
            jobs.append(self.send(sub.phone, body, kind, sub.channel))

        if jobs:
            await asyncio.gather(*jobs)
            self.manual_last_at = now
        return {"sent": len(jobs), "cooldown_left": 0}

    def subscriber_summary(self) -> list[dict]:
        """Masked view for the admin page — never returns a full phone number."""
        now = time.time()
        return [
            {
                "id": s.entry_id,
                "phone": mask(s.phone),
                "channel": s.channel,
                "rank": s.last_rank,
                "consented": time.strftime("%H:%M:%S", time.localtime(s.consent_at)),
                "cooldown_left": max(0, int(COOLDOWN_SEC - (now - s.last_notified_at)))
                if s.last_notified_at else 0,
                "grace_left": max(0, int(GRACE_SEC - (now - s.last_lap_at)))
                if s.last_lap_at else 0,
            }
            for s in self.subscribers.values()
        ]

    def recent(self, limit: int = 50) -> list[dict]:
        return [
            {
                "at": time.strftime("%H:%M:%S", time.localtime(m.at)),
                "to": mask(m.to),
                "kind": m.kind,
                "channel": m.channel,
                "delivered": m.delivered,
                "detail": m.detail,
                "body": m.body,
            }
            for m in self.log[-limit:][::-1]
        ]


engine = Engine()


def config() -> dict:
    """Surfaced in the admin page so it is obvious *why* a message was suppressed."""
    return {
        "dry_run": DRY_RUN,
        "manual_cooldown": Engine.MANUAL_COOLDOWN,
        "sender": ("messaging service " + TWILIO_SERVICE_SID[:8] + "…") if TWILIO_SERVICE_SID
                  else (TWILIO_FROM or "not configured"),
        "whatsapp": TWILIO_WHATSAPP_FROM or "not configured",
        "cooldown_sec": COOLDOWN_SEC,
        "grace_sec": GRACE_SEC,
        "gap_sec": GAP_SEC,
        "min_drop": MIN_DROP,
        "top_n": TOP_N,
    }


def valid_phone(phone: str) -> bool:
    return bool(PHONE_RE.match(phone.strip().replace(" ", "")))


def opt_out(sub: "Subscriber | None") -> str:
    """The opt-out fragment appended to every message."""
    if sub and sub.token and PUBLIC_URL:
        return f" Stop: {PUBLIC_URL}/stop?t={sub.token}"
    if sub and sub.token:
        return f" Stop: /stop?t={sub.token}"      # PUBLIC_URL not set yet
    return ""


def welcome_text(name: str, rank: int | None, total: int | None,
                 sub: "Subscriber | None" = None) -> str:
    where = f" You're currently #{rank} of {total}." if rank else ""
    return (
        f"Thanks {name} — you're on the DHL The Ring leaderboard.{where}"
        f" We'll text you your position during the event.{opt_out(sub)}"
    )


def position_text(rank: int, total: int, lap: str, sub: "Subscriber | None" = None) -> str:
    return (f"You're now #{rank} of {total} at DHL The Ring. Best lap {lap}."
            f"{opt_out(sub)}")


def summary_text(rank: int, total: int, lap: str, sub: "Subscriber | None" = None) -> str:
    return (f"You finished #{rank} of {total} at DHL The Ring. Fastest lap {lap}."
            f" Thanks for racing!{opt_out(sub)}")
