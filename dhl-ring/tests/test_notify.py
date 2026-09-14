"""
Tests for the notification decision logic — the part that decides when NOT to
send. Every case here corresponds to a way a naive implementation annoys people.
"""

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "cloud"))

import notify  # noqa: E402


def board(*rows):
    """rows = (id, rank, ms)"""
    out = []
    for entry_id, rank, ms in rows:
        m, r = divmod(ms, 60_000)
        s, ml = divmod(r, 1_000)
        out.append({"id": entry_id, "rank": rank, "name": entry_id.upper(),
                    "best_lap_ms": ms, "best_lap": f"{m}:{s:02d}.{ml:03d}"})
    return out


@pytest.fixture()
def engine():
    return notify.Engine()


def test_phone_validation():
    assert notify.valid_phone("+421900123456")
    assert notify.valid_phone("+49 151 12345678")
    assert not notify.valid_phone("0900123456")     # no country code
    assert not notify.valid_phone("+421")           # too short
    assert not notify.valid_phone("hello")


def test_first_sighting_is_silent(engine):
    engine.subscribe("a", "+421900000001")
    out = engine.evaluate(board(("a", 1, 55000)))
    assert out == []                                 # baseline only, never a message


def test_overtake_sends_one_message(engine):
    now = time.time()
    engine.subscribe("a", "+421900000001", now=now)
    engine.evaluate(board(("a", 1, 55000)), now=now)
    # they walk away, then someone beats them
    later = now + notify.GRACE_SEC + 10
    out = engine.evaluate(board(("b", 1, 54000), ("a", 2, 55000)), now=later)
    assert len(out) == 1
    sub, body = out[0]
    assert sub.phone == "+421900000001"
    assert "#2 of 2" in body and "was #1" in body
    assert "/stop?t=" in body                        # opt-out link on every message


def test_improving_never_triggers_a_message(engine):
    now = time.time()
    engine.subscribe("a", "+421900000001", now=now)
    engine.evaluate(board(("a", 3, 58000)), now=now)
    later = now + notify.GRACE_SEC + 10
    out = engine.evaluate(board(("a", 1, 54000)), now=later)
    assert out == []


def test_silent_while_still_driving(engine):
    """They are standing at the rig watching the screen — a text is noise."""
    now = time.time()
    engine.subscribe("a", "+421900000001", now=now)
    engine.evaluate(board(("a", 1, 55000)), now=now)
    # their own lap updates, then they are passed 30 seconds later
    engine.evaluate(board(("a", 1, 54500)), now=now + 10)
    out = engine.evaluate(board(("b", 1, 54000), ("a", 2, 54500)), now=now + 40)
    assert out == []


def test_rate_limited_over_a_long_session(engine):
    """
    A driver who keeps getting passed all afternoon must not be spammed. Run 40
    minutes of realistic ingests (every 30s) while they lose a place every
    minute, and check the messages are spaced at least one cooldown apart.
    """
    now = time.time()
    engine.subscribe("a", "+421900000001", now=now)

    sent_at = []
    rank = 1
    for step in range(80):                       # 80 ingests x 30s = 40 minutes
        t = now + step * 30
        if step > 0 and step % 2 == 0:           # someone passes them every minute
            rank += 1
        rows = [(f"r{i}", i + 1, 50_000 + i * 100) for i in range(rank - 1)]
        rows.append(("a", rank, 55_000))
        out = engine.evaluate(board(*rows), now=t)
        if out:
            sent_at.append(t)

    assert sent_at, "a driver dropping 40 places should hear something"
    gaps = [b - a for a, b in zip(sent_at, sent_at[1:])]
    assert all(g >= notify.COOLDOWN_SEC for g in gaps), f"messages too close: {gaps}"
    # 40 minutes at a 10 minute cooldown: a handful, not dozens
    assert len(sent_at) <= 40 * 60 / notify.COOLDOWN_SEC + 1
    assert len(sent_at) <= 5


def test_second_overtake_inside_cooldown_stays_silent(engine):
    now = time.time()
    engine.subscribe("a", "+421900000001", now=now)
    engine.evaluate(board(("a", 1, 55000)), now=now)

    t = now
    for _ in range(int(notify.GRACE_SEC / 30) + 2):        # let the grace period pass
        t += 30
        engine.evaluate(board(("a", 1, 55000)), now=t)

    t += 30
    assert len(engine.evaluate(board(("b", 1, 54000), ("a", 2, 55000)), now=t)) == 1
    t += 30
    out = engine.evaluate(board(("b", 1, 54000), ("c", 2, 54500), ("a", 3, 55000)), now=t)
    assert out == []                                       # inside cooldown: silent


def test_reconnect_after_outage_does_not_replay(engine):
    """The uplink was down for 20 minutes. Rebaseline, don't fire a burst."""
    now = time.time()
    for who in ("a", "b", "c"):
        engine.subscribe(who, "+42190000000" + who)
    engine.evaluate(board(("a", 1, 55000), ("b", 2, 56000), ("c", 3, 57000)), now=now)
    much_later = now + 20 * 60
    out = engine.evaluate(
        board(("x", 1, 51000), ("y", 2, 52000), ("z", 3, 53000),
              ("a", 4, 55000), ("b", 5, 56000), ("c", 6, 57000)), now=much_later)
    assert out == []
    # and the new positions became the baseline, so the next real drop still works
    t = much_later + notify.GRACE_SEC + 10
    out = engine.evaluate(
        board(("x", 1, 51000), ("y", 2, 52000), ("z", 3, 53000), ("w", 4, 54000),
              ("a", 5, 55000), ("b", 6, 56000), ("c", 7, 57000)), now=t)
    assert len(out) == 3


def test_driver_not_on_the_board_is_skipped(engine):
    engine.subscribe("ghost", "+421900000009")
    assert engine.evaluate(board(("a", 1, 55000))) == []


def test_unsubscribe(engine):
    now = time.time()
    engine.subscribe("a", "+421900000001", now=now)
    engine.evaluate(board(("a", 1, 55000)), now=now)
    assert engine.unsubscribe("a") is True
    out = engine.evaluate(board(("b", 1, 54000), ("a", 2, 55000)),
                          now=now + notify.GRACE_SEC + 10)
    assert out == []


def test_log_masks_the_phone_number(engine):
    import asyncio
    asyncio.run(engine.send("+421900123456", "hello", "welcome"))
    entry = engine.recent()[0]
    assert entry["to"].endswith("••••")
    assert "123456" not in entry["to"]


def test_welcome_text_mentions_position_and_optout(engine):
    sub = engine.subscribe("a", "+421900000001", rank=4)
    text = notify.welcome_text("D. PECL", 4, 57, sub)
    assert "#4 of 57" in text
    assert f"/stop?t={sub.token}" in text


# ---------------------------------------------------------------- opt-out link
def test_every_message_carries_an_opt_out_link(engine, monkeypatch):
    monkeypatch.setattr(notify, "PUBLIC_URL", "https://roarfun.live")
    now = time.time()
    sub = engine.subscribe("a", "+421900000001", now=now, rank=1, lap_ms=55000)
    welcome = notify.welcome_text("D. PECL", 1, 2, sub)
    assert f"https://roarfun.live/stop?t={sub.token}" in welcome
    assert "Reply STOP" not in welcome        # cannot work with an alphanumeric sender

    t = now
    for _ in range(int(notify.GRACE_SEC / 30) + 2):
        t += 30
        engine.evaluate(board(("a", 1, 55000)), now=t)
    t += 30
    out = engine.evaluate(board(("b", 1, 54000), ("a", 2, 55000)), now=t)
    assert len(out) == 1
    assert f"/stop?t={sub.token}" in out[0][1]


def test_messages_fit_one_sms_segment(engine, monkeypatch):
    """Long messages bill as two segments and look untidy. Keep under 160 chars."""
    monkeypatch.setattr(notify, "PUBLIC_URL",
                        "https://orbweaver-leaderboard-production.up.railway.app")
    sub = engine.subscribe("a", "+421900000001", rank=6, lap_ms=54203)
    welcome = notify.welcome_text("D. PECL", 6, 84, sub)
    body = (f"Your position changed: now #7 of 84 (was #6). Best lap 0:54.203."
            f"{notify.opt_out(sub)}")
    assert len(body) <= 160, f"overtake message is {len(body)} chars"
    # the welcome is longer; flag it if it would ever exceed two segments
    assert len(welcome) <= 306, f"welcome message is {len(welcome)} chars"


def test_opt_out_token_is_not_the_public_entry_id(engine):
    sub = engine.subscribe("aaa111", "+421900000001")
    assert sub.token and sub.token != "aaa111"
    assert len(sub.token) >= 10


def test_unsubscribe_by_token(engine):
    sub = engine.subscribe("a", "+421900000001")
    assert engine.unsubscribe_by_token("wrong-token") is False
    assert engine.unsubscribe_by_token(sub.token) is True
    assert engine.subscribers == {}
    assert engine.unsubscribe_by_token(sub.token) is False   # second use is dead
