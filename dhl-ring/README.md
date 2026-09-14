# DHL The Ring — 10th edition

Sim-racing leaderboard and SMS re-engagement for the DHL The Ring event.
Built on the RoarFun leaderboard framework, configured for this event.

Public address: **dhl.roarfun.live**

---

## What is different from the standard framework

| | This event |
|---|---|
| Names | **Full name shown** (private event) — "DAVID PECL", not "D. P." |
| Second field | **Country**, chosen from a dropdown, shown in full |
| Returning visitors | Found by **name and surname** |
| Wall display | **DHL yellow**, white panel, black text, **fullscreen button** |
| Notifications | **Manual only** — you press the button, nothing sends by itself |
| Backup | **CSV written to disk after every entry** |
| Dates | No date or schedule commitments anywhere — you control every send |

---

## Notifications: you are the trigger

Nothing sends automatically except the welcome SMS when someone opts in.

In `/admin` → **Subscribers** tab:

- **Send position update** — texts every opted-in driver their current standing:
  *"You're now #7 of 42 at DHL The Ring. Best lap 0:54.203."*
- **Send end-of-event summary** — *"You finished #7 of 42 at DHL The Ring."*

Both show how many people will receive it, ask for confirmation, and then lock
for **30 seconds** so a double-press cannot double-send. Anyone who used the
opt-out link is excluded automatically.

Send as many position updates during the day as you like.

---

## The wall display

Served by the booth PC itself at `/wall`, so it **works with no internet**.

- Fullscreen button, bottom right
- Top 10 at rest; a driver finishing outside the top 10 scrolls into view,
  holds for 10 seconds, then returns to the top
- `?top=15` to show more rows at rest

**Background image:** drop a file called `background.jpg` into `booth/assets/`.
Design at 3840×2160. The leaderboard panel sits in the middle, so keep the busy
artwork toward the edges. No file = flat DHL yellow, which also looks fine.

---

## Running it

1. Double-click **install.bat** once (Windows).
2. Double-click **DHL Booth** to open the control panel.
3. Enter the cloud address and token, Save settings, Start the booth.
4. Type the shown address into each tablet.
5. Press **Open wall display** for the screen, then its fullscreen button.

On first run Windows asks about the firewall — allow **Private networks**, or
the tablets cannot reach the hub. Turn off sleep in the power settings.

---

## Backups

Every registration and every lap is appended immediately to
`booth/entries_backup.csv` — plain text, opens in Excel, in the order it
happened. This is independent of the database: if the database is ever lost,
every entry is still recoverable from this file.

The **Export visitors (CSV)** button in the control panel gives the full
current state at any time.

---

## Tests

```bash
pip install -r cloud/requirements.txt -r booth/requirements.txt pytest httpx
python -m pytest -q
```

107 tests, including DHL-specific coverage: full names, the country dropdown,
manual-send behaviour, the 30-second cooldown, opt-out being honoured, and that
the backup CSV survives a total database loss.

`tests/live_test_dhl.sh` runs the whole chain as real processes, including
killing the cloud mid-session to prove the booth carries on.

---

## Known limits

Cloud state is in memory unless `REDIS_URL` is set — a redeploy clears
subscribers. The booth re-syncs opt-ins within 30 seconds.

Admin access is a single shared token.
