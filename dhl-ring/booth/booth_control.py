"""
RoarFun Booth Control — the only thing an operator touches.

Double-click to open. Starts the local hub and the cloud mirror inside this same
process (threads, not terminals), shows what is happening, and gives the tablet
address to type in. Nothing here needs a command line, and nothing here needs
the internet: if the uplink is down, the hub and the tablets carry on and the
mirror simply retries.

    python booth_control.py        (or double-click RoarFun Booth.bat / .command)
"""

from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import requests

from control_core import (DEFAULT_PORT, digest, load_settings, local_ip,
                          next_backoff, save_settings, tablet_url, valid_port)

HERE = Path(__file__).resolve().parent
SETTINGS_FILE = HERE / "booth_settings.json"

BG = "#0B0F14"
PANEL = "#141B24"
LINE = "#2A3542"
TXT = "#E9EDF2"
DIM = "#7D8B9E"
OK = "#3FB56B"
WARN = "#D8A13A"
BAD = "#E8342A"


class Control(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("DHL The Ring — Booth Control")
        self.configure(bg=BG)
        self.minsize(760, 620)

        self.log_queue: queue.Queue = queue.Queue()
        self.hub_server = None
        self.hub_thread: threading.Thread | None = None
        self.pusher_stop = threading.Event()
        self.pusher_thread: threading.Thread | None = None
        self.cloud_ok: bool | None = None
        self.last_push_ok: bool | None = None

        self._vars()
        self._style()
        self._build()
        self._load()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(300, self._drain)
        self.after(1000, self._tick)

    # ------------------------------------------------------------- appearance
    def _vars(self) -> None:
        self.var_port = tk.StringVar(value=str(DEFAULT_PORT))
        self.var_cloud = tk.StringVar(value="https://dhl.roarfun.live")
        self.var_token = tk.StringVar()
        self.var_event = tk.StringVar(value="dhl-the-ring")
        self.var_pin = tk.StringVar(value="1234")
        self.var_mirror = tk.BooleanVar(value=True)
        self.var_hub_state = tk.StringVar(value="Stopped")
        self.var_mirror_state = tk.StringVar(value="Not running")
        self.var_stats = tk.StringVar(value="—")
        self.var_tablet = tk.StringVar(value="Start the booth to see the tablet address")

    def _style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=TXT, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=PANEL, relief="flat")
        style.configure("TLabel", background=BG, foreground=TXT, font=("Helvetica", 11))
        style.configure("Card.TLabel", background=PANEL, foreground=TXT)
        style.configure("Dim.TLabel", background=PANEL, foreground=DIM, font=("Helvetica", 10))
        style.configure("Head.TLabel", background=BG, foreground=DIM,
                        font=("Helvetica", 9, "bold"))
        style.configure("Big.TLabel", background=PANEL, foreground=TXT,
                        font=("Helvetica", 15, "bold"))
        style.configure("TButton", background="#1B242F", foreground=TXT,
                        borderwidth=0, padding=(14, 9), font=("Helvetica", 11))
        style.map("TButton", background=[("active", "#26313F")])
        style.configure("Go.TButton", background=BAD, foreground="#fff",
                        font=("Helvetica", 12, "bold"), padding=(18, 12))
        style.map("Go.TButton", background=[("active", "#c22c23")])
        style.configure("TEntry", fieldbackground="#0E131A", foreground=TXT,
                        insertcolor=TXT, borderwidth=1)
        style.configure("TCheckbutton", background=PANEL, foreground=DIM)

    def _card(self, parent, title):
        wrap = ttk.Frame(parent, style="TFrame")
        ttk.Label(wrap, text=title.upper(), style="Head.TLabel").pack(anchor="w", pady=(0, 4))
        card = tk.Frame(wrap, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        card.pack(fill="both", expand=True)
        return wrap, card

    # ------------------------------------------------------------------ layout
    def _build(self) -> None:
        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)

        # --- big status row
        wrap, card = self._card(outer, "Status")
        wrap.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure((0, 1), weight=1)

        self.hub_dot = tk.Canvas(card, width=13, height=13, bg=PANEL, highlightthickness=0)
        self.hub_dot.grid(row=0, column=0, sticky="w", padx=(14, 0), pady=(14, 0))
        self.hub_oval = self.hub_dot.create_oval(2, 2, 11, 11, fill=DIM, outline="")
        ttk.Label(card, textvariable=self.var_hub_state, style="Big.TLabel").grid(
            row=0, column=0, sticky="w", padx=(34, 0), pady=(12, 0))
        ttk.Label(card, text="Booth  ·  tablets and wall display",
                  style="Dim.TLabel").grid(row=1, column=0, sticky="w", padx=14)

        self.mir_dot = tk.Canvas(card, width=13, height=13, bg=PANEL, highlightthickness=0)
        self.mir_dot.grid(row=0, column=1, sticky="w", pady=(14, 0))
        self.mir_oval = self.mir_dot.create_oval(2, 2, 11, 11, fill=DIM, outline="")
        ttk.Label(card, textvariable=self.var_mirror_state, style="Big.TLabel").grid(
            row=0, column=1, sticky="w", padx=(20, 0), pady=(12, 0))
        ttk.Label(card, text="Cloud mirror  ·  public feed and messages",
                  style="Dim.TLabel").grid(row=1, column=1, sticky="w")

        ttk.Label(card, textvariable=self.var_stats, style="Card.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 14))

        # --- start / stop
        bar = ttk.Frame(outer)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        self.btn_go = ttk.Button(bar, text="Start the booth", style="Go.TButton",
                                 command=self.toggle)
        self.btn_go.pack(side="left")
        ttk.Button(bar, text="Open tablet page", command=self.open_tablet).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Open wall display", command=self.open_display).pack(side="left", padx=(8, 0))
        ttk.Button(bar, text="Export visitors (CSV)", command=self.export).pack(side="left", padx=(8, 0))

        # --- tablet address
        wrap, card = self._card(outer, "Type this on each tablet")
        wrap.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        ttk.Label(card, textvariable=self.var_tablet, style="Big.TLabel").pack(
            anchor="w", padx=14, pady=(12, 2))
        ttk.Label(card, text="Both tablets must be on the booth's own WiFi, not the hall network.",
                  style="Dim.TLabel").pack(anchor="w", padx=14, pady=(0, 12))

        # --- settings
        wrap, card = self._card(outer, "Settings")
        wrap.grid(row=3, column=0, sticky="ew", pady=(0, 12))
        card.columnconfigure(1, weight=1)
        rows = [("Cloud address", self.var_cloud, False),
                ("Access token", self.var_token, True),
                ("Event name", self.var_event, False),
                ("Staff PIN (to undo a lap)", self.var_pin, False),
                ("Port", self.var_port, False)]
        for i, (label, var, secret) in enumerate(rows):
            ttk.Label(card, text=label, style="Dim.TLabel").grid(
                row=i, column=0, sticky="w", padx=(14, 10), pady=5)
            ttk.Entry(card, textvariable=var, show="•" if secret else "").grid(
                row=i, column=1, sticky="ew", padx=(0, 14), pady=5)
        ttk.Checkbutton(card, text="Mirror to the cloud (uncheck to run fully offline)",
                        variable=self.var_mirror).grid(row=len(rows), column=1,
                                                       sticky="w", pady=(2, 4))
        actions = ttk.Frame(card, style="Card.TFrame")
        actions.grid(row=len(rows) + 1, column=1, sticky="w", pady=(0, 12))
        ttk.Button(actions, text="Save settings", command=self._save).pack(side="left")
        ttk.Button(actions, text="Test cloud", command=self.test_cloud).pack(side="left", padx=8)

        # --- log
        wrap, card = self._card(outer, "Activity")
        wrap.grid(row=4, column=0, sticky="nsew")
        outer.rowconfigure(4, weight=1)
        self.log = tk.Text(card, height=10, bg=PANEL, fg=TXT, bd=0, wrap="none",
                           font=("Menlo", 10), state="disabled", padx=12, pady=10)
        self.log.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(card, command=self.log.yview)
        scroll.pack(side="right", fill="y")
        self.log.configure(yscrollcommand=scroll.set)
        self.log.tag_configure("warn", foreground=WARN)
        self.log.tag_configure("bad", foreground=BAD)
        self.log.tag_configure("ok", foreground=OK)

    # -------------------------------------------------------------- utilities
    def say(self, text: str, tag: str = "") -> None:
        self.log_queue.put((text, tag))

    def _drain(self) -> None:
        while not self.log_queue.empty():
            text, tag = self.log_queue.get()
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(300, self._drain)

    def _dot(self, canvas, oval, colour) -> None:
        canvas.itemconfig(oval, fill=colour)

    def base_url(self) -> str:
        return f"http://127.0.0.1:{valid_port(self.var_port.get())}"

    # -------------------------------------------------------------- settings
    def _save(self) -> None:
        data = {"cloud": self.var_cloud.get(), "token": self.var_token.get(),
                "event": self.var_event.get(), "pin": self.var_pin.get(),
                "port": self.var_port.get(), "mirror": self.var_mirror.get()}
        if save_settings(SETTINGS_FILE, data):
            self.say("Settings saved.", "ok")
        else:
            messagebox.showerror("Could not save", f"Cannot write {SETTINGS_FILE}")

    def _load(self) -> None:
        data = load_settings(SETTINGS_FILE)
        self.var_cloud.set(data["cloud"])
        self.var_token.set(data["token"])
        self.var_event.set(data["event"])
        self.var_pin.set(data["pin"])
        self.var_port.set(str(data["port"]))
        self.var_mirror.set(bool(data["mirror"]))

    # ------------------------------------------------------------ the booth
    def toggle(self) -> None:
        if self.hub_thread and self.hub_thread.is_alive():
            self.stop()
        else:
            self.start()

    def start(self) -> None:
        port = valid_port(self.var_port.get())
        os.environ["HUB_DB"] = str(HERE / "booth.db")
        os.environ["EVENT_ID"] = self.var_event.get().strip() or "event"
        os.environ["STAFF_PIN"] = self.var_pin.get().strip() or "1234"

        try:
            import uvicorn
            import hub as hub_module
        except Exception as exc:
            messagebox.showerror(
                "Missing components",
                f"Could not load the booth software:\n\n{exc}\n\n"
                "Make sure hub.py is in the same folder and the requirements are installed.")
            return

        config = uvicorn.Config(hub_module.app, host="0.0.0.0", port=port, log_level="error")
        self.hub_server = uvicorn.Server(config)
        self.hub_thread = threading.Thread(target=self.hub_server.run, daemon=True)
        self.hub_thread.start()

        address = tablet_url(port)
        self.var_tablet.set(address)
        self.var_hub_state.set("Running")
        self._dot(self.hub_dot, self.hub_oval, OK)
        self.btn_go.configure(text="Stop the booth")
        self.say(f"Booth started. Tablets: {address}", "ok")

        if self.var_mirror.get():
            self.start_mirror()

    def stop(self) -> None:
        self.pusher_stop.set()
        if self.hub_server:
            self.hub_server.should_exit = True
        self.hub_thread = None
        self.var_hub_state.set("Stopped")
        self.var_mirror_state.set("Not running")
        self._dot(self.hub_dot, self.hub_oval, DIM)
        self._dot(self.mir_dot, self.mir_oval, DIM)
        self.btn_go.configure(text="Start the booth")
        self.var_tablet.set("Start the booth to see the tablet address")
        self.say("Booth stopped.", "warn")

    # ------------------------------------------------------------- the mirror
    def start_mirror(self) -> None:
        if not self.var_token.get().strip():
            self.say("No access token set — running offline only.", "warn")
            self.var_mirror_state.set("Offline (no token)")
            self._dot(self.mir_dot, self.mir_oval, WARN)
            return
        self.pusher_stop = threading.Event()
        self.pusher_thread = threading.Thread(target=self._mirror_loop, daemon=True)
        self.pusher_thread.start()
        self.var_mirror_state.set("Starting…")
        self._dot(self.mir_dot, self.mir_oval, WARN)

    def _mirror_loop(self) -> None:
        """Same behaviour as pusher_hub.py, run in-process so there is no terminal."""
        import time

        session = requests.Session()
        board_digest = subs_digest = None
        last_ok = 0.0
        pending = False
        backoff = 0.0
        next_attempt = 0.0
        last_subs = 0.0

        while not self.pusher_stop.is_set():
            cloud = self.var_cloud.get().strip().rstrip("/")
            token = self.var_token.get().strip()
            headers = {"Authorization": f"Bearer {token}"}
            try:
                board = session.get(f"{self.base_url()}/api/leaderboard", timeout=8).json()
                fingerprint = digest(board["leaderboard"])
                if fingerprint != board_digest:
                    board_digest = fingerprint
                    pending = True
                if time.monotonic() - last_ok > 60:
                    pending = True

                if pending and time.monotonic() >= next_attempt:
                    response = session.post(f"{cloud}/ingest", json=board,
                                            headers=headers, timeout=8)
                    if response.status_code < 400:
                        pending = False
                        backoff = 0.0
                        last_ok = time.monotonic()
                        self.last_push_ok = True
                        self.say(f"Mirrored {board['count']} entries to the cloud.")
                    else:
                        self.last_push_ok = False
                        backoff = next_backoff(backoff)
                        next_attempt = time.monotonic() + backoff
                        self.say(f"Cloud rejected the push ({response.status_code}); "
                                 f"retrying in {backoff:.0f}s", "bad")

                if time.monotonic() - last_subs > 30:
                    last_subs = time.monotonic()
                    subs = session.get(f"{self.base_url()}/api/subscribers",
                                       timeout=8).json()["subscribers"]
                    sd = digest(subs)
                    if sd != subs_digest:
                        r = session.post(f"{cloud}/sync/subscribers",
                                         json={"subscribers": subs}, headers=headers, timeout=8)
                        if r.status_code < 400:
                            subs_digest = sd
                            self.say(f"Synced {len(subs)} opt-ins.")
            except requests.RequestException:
                self.last_push_ok = False
                backoff = next_backoff(backoff)
                next_attempt = time.monotonic() + backoff
                self.say(f"No connection to the cloud; retrying in {backoff:.0f}s. "
                         "The booth is unaffected.", "warn")
            except Exception as exc:
                self.say(f"Mirror error: {exc}", "bad")
            self.pusher_stop.wait(4)

    # ---------------------------------------------------------------- actions
    def open_tablet(self) -> None:
        if not (self.hub_thread and self.hub_thread.is_alive()):
            messagebox.showinfo("Not running", "Start the booth first.")
            return
        webbrowser.open(f"{self.base_url()}/tablet")

    def open_display(self) -> None:
        if not (self.hub_thread and self.hub_thread.is_alive()):
            messagebox.showinfo("Not running", "Start the booth first.")
            return
        # The LOCAL wall display, served by the hub — works with no internet.
        # This is the screen above the bar; it must not depend on the cloud.
        webbrowser.open(f"{self.base_url()}/wall")

    def export(self) -> None:
        if not (self.hub_thread and self.hub_thread.is_alive()):
            messagebox.showinfo("Not running", "Start the booth first.")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            initialfile="visitors.csv",
                                            filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            data = requests.get(f"{self.base_url()}/api/export.csv", timeout=15).text
            Path(path).write_text(data, encoding="utf-8")
            self.say(f"Exported to {path}", "ok")
        except Exception as exc:
            messagebox.showerror("Export failed", str(exc))

    def test_cloud(self) -> None:
        cloud = self.var_cloud.get().strip().rstrip("/")
        try:
            r = requests.get(f"{cloud}/healthz", timeout=8)
            self.cloud_ok = r.ok
            self.say(f"Cloud replied {r.status_code}.", "ok" if r.ok else "bad")
        except Exception as exc:
            self.cloud_ok = False
            self.say(f"Cannot reach the cloud: {exc}", "bad")

    # ------------------------------------------------------------------ ticks
    def _tick(self) -> None:
        if self.hub_thread and self.hub_thread.is_alive():
            try:
                stats = requests.get(f"{self.base_url()}/api/stats", timeout=3).json()
                self.var_stats.set(
                    f"{stats['today']} on today's board   ·   {stats['visitors']} visitors "
                    f"registered   ·   {stats['runs']} laps   ·   {stats['opted_in']} opted in")
            except Exception:
                self.var_stats.set("Booth starting…")
            if self.pusher_thread and self.pusher_thread.is_alive():
                if self.last_push_ok is True:
                    self.var_mirror_state.set("Connected")
                    self._dot(self.mir_dot, self.mir_oval, OK)
                elif self.last_push_ok is False:
                    self.var_mirror_state.set("Retrying")
                    self._dot(self.mir_dot, self.mir_oval, WARN)
        self.after(2000, self._tick)

    def on_close(self) -> None:
        if self.hub_thread and self.hub_thread.is_alive():
            if not messagebox.askyesno("Close?",
                                       "The booth is running. Tablets will stop working.\n\n"
                                       "Close anyway?"):
                return
        self.pusher_stop.set()
        if self.hub_server:
            self.hub_server.should_exit = True
        self.destroy()


if __name__ == "__main__":
    Control().mainloop()
