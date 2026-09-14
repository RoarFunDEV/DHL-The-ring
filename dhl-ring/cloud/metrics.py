"""
Metrics for the operations dashboard.

Deliberately in-memory and bounded: 120 one-minute buckets, a few counters, and
a short ring of recent errors. No database, no external service, no measurable
cost. Enough to answer the questions that actually come up during a show —
is data still arriving, are messages going out, is anything failing — without
becoming a monitoring product.

Everything resets on redeploy. That is a known limitation, shared with the rest
of the cloud state.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

WINDOW_MINUTES = 120
STARTED_AT = time.time()


def minute_bucket(ts: float | None = None) -> int:
    return int((ts or time.time()) // 60)


@dataclass
class Bucket:
    minute: int
    requests: int = 0
    feed_hits: int = 0          # full leaderboard responses served
    feed_304: int = 0           # revalidations answered empty
    ingests: int = 0
    errors: int = 0
    bytes_out: int = 0
    messages: int = 0


@dataclass
class Metrics:
    buckets: deque[Bucket] = field(default_factory=lambda: deque(maxlen=WINDOW_MINUTES))
    by_path: dict[str, int] = field(default_factory=dict)
    status_counts: dict[str, int] = field(default_factory=dict)
    recent_errors: deque = field(default_factory=lambda: deque(maxlen=25))
    latencies_ms: deque = field(default_factory=lambda: deque(maxlen=500))
    last_ingest_at: float | None = None
    last_ingest_count: int = 0
    ingest_total: int = 0
    messages_sent: int = 0
    messages_failed: int = 0

    # ---------------------------------------------------------------- writing
    def _bucket(self) -> Bucket:
        now = minute_bucket()
        if not self.buckets or self.buckets[-1].minute != now:
            self.buckets.append(Bucket(minute=now))
        return self.buckets[-1]

    def record_request(self, path: str, status: int, ms: float, size: int = 0) -> None:
        bucket = self._bucket()
        bucket.requests += 1
        bucket.bytes_out += size
        self.latencies_ms.append(ms)
        self.by_path[path] = self.by_path.get(path, 0) + 1
        family = f"{status // 100}xx"
        self.status_counts[family] = self.status_counts.get(family, 0) + 1

        if path.startswith("/v1/leaderboard"):
            if status == 304:
                bucket.feed_304 += 1
            elif status == 200:
                bucket.feed_hits += 1
        if status >= 500:
            bucket.errors += 1
            self.recent_errors.append(
                {"at": time.strftime("%H:%M:%S"), "path": path, "status": status})

    def record_ingest(self, count: int) -> None:
        self._bucket().ingests += 1
        self.last_ingest_at = time.time()
        self.last_ingest_count = count
        self.ingest_total += 1

    def record_message(self, delivered: bool) -> None:
        self._bucket().messages += 1
        if delivered:
            self.messages_sent += 1
        else:
            self.messages_failed += 1

    # ---------------------------------------------------------------- reading
    def series(self, minutes: int = 60) -> list[dict]:
        """Per-minute history, gap-filled so a chart has no holes."""
        now = minute_bucket()
        known = {b.minute: b for b in self.buckets}
        out = []
        for m in range(now - minutes + 1, now + 1):
            b = known.get(m)
            out.append({
                "minute": m,
                "requests": b.requests if b else 0,
                "feed_hits": b.feed_hits if b else 0,
                "feed_304": b.feed_304 if b else 0,
                "ingests": b.ingests if b else 0,
                "errors": b.errors if b else 0,
                "bytes_out": b.bytes_out if b else 0,
                "messages": b.messages if b else 0,
            })
        return out

    def snapshot(self) -> dict:
        latencies = sorted(self.latencies_ms)
        recent = self.series(60)
        window_requests = sum(r["requests"] for r in recent)
        window_304 = sum(r["feed_304"] for r in recent)
        window_feed = window_304 + sum(r["feed_hits"] for r in recent)
        age = (time.time() - self.last_ingest_at) if self.last_ingest_at else None
        return {
            "uptime_sec": int(time.time() - STARTED_AT),
            "last_ingest_age_sec": round(age, 1) if age is not None else None,
            "last_ingest_count": self.last_ingest_count,
            "ingest_total": self.ingest_total,
            "requests_last_hour": window_requests,
            "requests_per_min": round(window_requests / 60, 1),
            "bytes_last_hour": sum(r["bytes_out"] for r in recent),
            # The share of feed requests answered without a body — the single
            # number that shows whether the bandwidth work is paying off.
            "revalidation_rate": round(window_304 / window_feed, 3) if window_feed else None,
            "latency_p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else None,
            "latency_p95_ms": (round(latencies[int(len(latencies) * 0.95)], 1)
                               if len(latencies) > 20 else None),
            "errors_last_hour": sum(r["errors"] for r in recent),
            "messages_sent": self.messages_sent,
            "messages_failed": self.messages_failed,
            "status_counts": dict(sorted(self.status_counts.items())),
            "top_paths": dict(sorted(self.by_path.items(),
                                     key=lambda kv: -kv[1])[:8]),
            "recent_errors": list(self.recent_errors)[::-1],
            "series": recent,
        }


metrics = Metrics()
