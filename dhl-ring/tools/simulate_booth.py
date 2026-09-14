"""
Rehearsal tool — drives the booth hub the way two real assistants would.

Registers visitors, records laps, opts some of them in, and occasionally sends a
returning visitor back for a second run. Use it to exercise the whole chain
before the show, ideally with the pusher running against a throttled uplink.

    python tools/simulate_booth.py                       # gentle, runs forever
    python tools/simulate_booth.py --visitors 150 --interval 2
    python tools/simulate_booth.py --hub http://192.168.1.50:8000
"""

from __future__ import annotations

import argparse
import random
import sys
import time

import requests

FIRST = ["Anna", "Markus", "Petra", "Jurgen", "Lukas", "David", "Tomas", "Zdenek",
         "Sofia", "Miguel", "Chen", "Yuki", "Elif", "Piotr", "Klaus", "Ingrid"]
COMPANIES = ["Siemens", "Vodafone", "DBAG", "Bosch", "SAP", "Infineon", "Continental",
             "T-Mobile", "LBBW Bank", "Alternetivo", "Security A", "Freelance"]
PREFIXES = ["+49", "+43", "+420", "+421", "+48", "+39", "+33", "+31", "+44", "+1"]


def register(hub: str, session: requests.Session) -> dict | None:
    first = random.choice(FIRST)
    body = {
        "first_name": first,
        "surname": random.choice("ABCDEFGHIJKLMNOPRSTVWZ"),
        "company": random.choice(COMPANIES),
        "actor": f"rig{random.randint(1, 2)}",
    }
    try:
        response = session.post(f"{hub}/api/visitors", json=body, timeout=8)
        response.raise_for_status()
        data = response.json()
        data["first_name"] = first
        return data
    except requests.RequestException as exc:
        print(f"  ! register failed: {exc}")
        return None


def drive(hub: str, session: requests.Session, visitor: dict, improve: bool = False) -> None:
    lap = random.randint(52_000, 78_000)
    if improve:
        lap = random.randint(50_000, 60_000)
    try:
        response = session.post(f"{hub}/api/runs", json={
            "visitor_id": visitor["id"], "lap_ms": lap,
            "actor": f"rig{random.randint(1, 2)}"}, timeout=8)
        response.raise_for_status()
        result = response.json()
        flag = "PB " if result["improved"] else "   "
        rank = f"P{result['rank']}" if result.get("rank") else "--"
        print(f"  {flag}{visitor['display']:8} {result['best_lap']:>9}  {rank:>4} "
              f"of {result.get('total', '?')}")
    except requests.RequestException as exc:
        print(f"  ! lap failed: {exc}")


def opt_in(hub: str, session: requests.Session, visitor: dict) -> None:
    phone = random.choice(PREFIXES) + str(random.randint(600_000_000, 799_999_999))
    try:
        session.post(f"{hub}/api/consent", json={
            "visitor_id": visitor["id"], "phone": phone,
            "channel": random.choice(["sms", "whatsapp"]), "consent": True}, timeout=8)
    except requests.RequestException:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Drive the booth hub like a real session")
    parser.add_argument("--hub", default="http://127.0.0.1:8000")
    parser.add_argument("--visitors", type=int, default=0,
                        help="stop after this many registrations (0 = run forever)")
    parser.add_argument("--interval", type=float, default=6.0,
                        help="seconds between drives (a real run is ~5 min)")
    parser.add_argument("--optin-rate", type=float, default=0.55)
    parser.add_argument("--return-rate", type=float, default=0.2,
                        help="chance a drive is a returning visitor rather than a new one")
    args = parser.parse_args()

    session = requests.Session()
    try:
        session.get(f"{args.hub}/healthz", timeout=5).raise_for_status()
    except requests.RequestException as exc:
        print(f"Cannot reach the hub at {args.hub}: {exc}")
        return 2

    print(f"Driving {args.hub} — one lap every {args.interval}s. Ctrl+C to stop.\n")
    known: list[dict] = []
    registered = 0

    while True:
        returning = known and random.random() < args.return_rate
        if returning:
            visitor = random.choice(known)
            drive(args.hub, session, visitor, improve=True)
        else:
            visitor = register(args.hub, session)
            if visitor is None:
                time.sleep(args.interval)
                continue
            registered += 1
            known.append(visitor)
            drive(args.hub, session, visitor)
            if random.random() < args.optin_rate:
                opt_in(args.hub, session, visitor)

        if args.visitors and registered >= args.visitors:
            print(f"\nDone — {registered} visitors registered.")
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
