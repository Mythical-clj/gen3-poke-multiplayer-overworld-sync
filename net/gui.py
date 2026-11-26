"""gblink.exe: a standalone desktop window (no browser).

The same engine as pannello.py (it starts relay.py, mb_multi.py, client.py,
usb_link.py as child processes), with a native window on top.

When the exe is started as `gblink.exe -u script.py args` it runs that script
instead of opening the window: that is how the window starts its children.
"""
import os
import runpy
import sys


def _run_child():
    """gblink.exe -u <script.py> <args...>: run a helper script and exit."""
    script = sys.argv[2]
    sys.argv = sys.argv[2:]
    try:
        sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
        sys.stderr.reconfigure(line_buffering=True, encoding="utf-8")
    except Exception:
        pass
    runpy.run_path(script, run_name="__main__")


if len(sys.argv) >= 3 and sys.argv[1] == "-u" and sys.argv[2].endswith(".py"):
    _run_child()
    sys.exit(0)

# Settings and logs live in the user's folder, not next to the exe (a onefile
# exe unpacks to a temporary folder that disappears when it closes).
DATA = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "gblink")
os.makedirs(DATA, exist_ok=True)
os.environ.setdefault("PANNELLO_CONFIG", os.path.join(DATA, "config.json"))
os.environ.setdefault("PANNELLO_LOG", os.path.join(DATA, "log"))
STUB_MEMO = os.path.join(DATA, "mbstub-path.txt")

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

if False:  # never runs: lets PyInstaller see what the helper scripts import
    import argparse, base64, hashlib, heapq, json, queue, random, re, select  # noqa
    import selectors, socket, ssl, struct, subprocess, tempfile, threading  # noqa
    import time, urllib.error, urllib.parse, urllib.request, collections  # noqa
    import usb.core, usb.util, usb.backend.libusb1, libusb_package  # noqa
    import protocol, usb_link, ws_link, club_link, club_mgba, finto_club  # noqa

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import pannello

GREEN = "#0f6b4a"
RED = "#b3392f"
AMBER = "#c07a12"
TAG_COLORS = {"relay": "#2f6fd0", "gioca": "#0f8a5a", "multiboot": "#b07a00",
              "sblocca": "#c04040", "finto": "#8a4bc0"}


def find_stub():
    cands = [os.environ.get("MBSTUB_PATH") or ""]
    try:
        with open(STUB_MEMO, "r", encoding="utf-8") as f:
            cands.append(f.read().strip())
    except OSError:
        pass
    exe_dir = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    cands += [os.path.join(exe_dir, "mbstub.gba"),
              os.path.join(exe_dir, "net", "mbstub.gba"),
              os.path.join(DATA, "mbstub.gba"),
              os.path.join(BASE, "mbstub.gba")]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


class App:
    def __init__(self, root):
        self.root = root
        self.p = pannello.Pannello()
        self.seq = 0
        self.dirty = False
        root.title("gblink - GBA multiplayer link")
        root.geometry("1040x760")
        root.minsize(900, 640)
        root.protocol("WM_DELETE_WINDOW", self.close)

        style = ttk.Style()
        try:
            style.theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass

        self.build_status()
        self.build_body()
        self.build_log()
        self.load_config_into_fields()
        self.tick()

    # --- layout ---------------------------------------------------------

    def build_status(self):
        f = ttk.Frame(self.root, padding=(10, 8))
        f.pack(fill="x")
        self.st_gba = tk.StringVar(value="Your GBA: stopped")
        self.st_friend = tk.StringVar(value="Friend: waiting")
        self.st_chan = tk.StringVar(value="Channel: -")
        self.st_club = tk.StringVar(value="")
        for var in (self.st_gba, self.st_friend, self.st_chan):
            ttk.Label(f, textvariable=var, font=("Segoe UI", 11, "bold")).pack(side="left", padx=(0, 24))
        ttk.Label(f, textvariable=self.st_club, foreground=GREEN).pack(side="left")
        self.warn = tk.StringVar(value="")
        self.warn_label = tk.Label(self.root, textvariable=self.warn, fg="white", bg=RED,
                                   anchor="w", padx=10, pady=4, wraplength=1000, justify="left")

    def build_body(self):
        body = ttk.Frame(self.root, padding=(10, 0))
        body.pack(fill="x")

        game = ttk.LabelFrame(body, text="Game", padding=10)
        game.pack(side="left", fill="both", expand=True, padx=(0, 8))
        ttk.Label(game, text="Before pressing anything: cartridge slot EMPTY, cable plugged in,\n"
                             "then turn the GBA on. After the load, insert the cartridge while it runs.",
                  justify="left").pack(anchor="w", pady=(0, 8))
        self.buttons = {}
        for name, num, title, hint in (
            ("relay", "0", "Start the relay", "Only needed by the host. Leave it running."),
            ("multiboot", "1", "Load the game into the GBA", "Red screen, insert cartridge, green."),
            ("gioca", "2", "Play", "Press when you are in the game, outdoors."),
        ):
            row = ttk.Frame(game)
            row.pack(fill="x", pady=3)
            b = ttk.Button(row, text="Start", width=8, command=lambda n=name: self.toggle(n))
            b.pack(side="right")
            ttk.Label(row, text="%s  %s" % (num, title), font=("Segoe UI", 10, "bold")).pack(anchor="w")
            ttk.Label(row, text=hint, foreground="#666").pack(anchor="w")
            self.buttons[name] = b
        tools = ttk.Frame(game)
        tools.pack(fill="x", pady=(10, 0))
        ttk.Button(tools, text="Unjam adapter", command=lambda: self.start("sblocca")).pack(side="left", padx=(0, 6))
        self.btn_finto = ttk.Button(tools, text="Test alone (fake partner)", command=lambda: self.toggle("finto"))
        self.btn_finto.pack(side="left", padx=(0, 6))
        ttk.Button(tools, text="Choose mbstub.gba...", command=self.pick_stub).pack(side="left")
        self.action_msg = tk.StringVar(value="")
        ttk.Label(game, textvariable=self.action_msg, foreground=RED, wraplength=430, justify="left").pack(anchor="w", pady=(8, 0))

        sett = ttk.LabelFrame(body, text="Settings", padding=10)
        sett.pack(side="left", fill="both", expand=True)
        ttk.Label(sett, text="These must match between you and your friend: the same room and relay.",
                  wraplength=430, justify="left").pack(anchor="w", pady=(0, 6))
        self.role = tk.StringVar(value="ospite")
        r = ttk.Frame(sett)
        r.pack(fill="x")
        ttk.Radiobutton(r, text="I host the relay", variable=self.role, value="ospite",
                        command=self.role_changed).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(r, text="I connect to a friend", variable=self.role, value="amico",
                        command=self.role_changed).pack(side="left")
        grid = ttk.Frame(sett)
        grid.pack(fill="x", pady=6)
        self.v_relay = tk.StringVar()
        self.v_room = tk.StringVar()
        self.v_timing = tk.StringVar()
        self.v_peer = tk.StringVar()
        self.v_cable = tk.StringVar()
        rows = (("Relay (host:port or wss://...)", self.v_relay, None),
                ("Room (same number for everyone)", self.v_room, None),
                ("Channel speed (timing, leave at 7400)", self.v_timing, None),
                ("Your number (peer, 0 = automatic)", self.v_peer, None),
                ("Cable", self.v_cable, ("auto", "gba", "gbc")))
        for i, (label, var, choices) in enumerate(rows):
            ttk.Label(grid, text=label).grid(row=i, column=0, sticky="w", pady=2, padx=(0, 10))
            if choices:
                w = ttk.Combobox(grid, textvariable=var, values=choices, state="readonly", width=30)
            else:
                w = ttk.Entry(grid, textvariable=var, width=33)
            w.grid(row=i, column=1, sticky="w")
            var.trace_add("write", lambda *a: setattr(self, "dirty", True))
        ttk.Label(sett, text="With 3 or more players (even mixing GBAs, emulators, browsers) everyone\n"
                             "needs a DIFFERENT peer number.", foreground="#666", justify="left").pack(anchor="w")
        sv = ttk.Frame(sett)
        sv.pack(fill="x", pady=(8, 0))
        ttk.Button(sv, text="Save settings", command=self.save).pack(side="left")
        self.cfg_msg = tk.StringVar(value="")
        ttk.Label(sv, textvariable=self.cfg_msg).pack(side="left", padx=10)

    def build_log(self):
        box = ttk.LabelFrame(self.root, text="Log", padding=6)
        box.pack(fill="both", expand=True, padx=10, pady=10)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        self.follow = tk.BooleanVar(value=True)
        self.only_game = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Follow new lines", variable=self.follow).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(bar, text="Game only", variable=self.only_game, command=self.clear_log).pack(side="left", padx=(0, 12))
        ttk.Button(bar, text="Copy the whole log", command=self.copy_log).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Save log...", command=self.save_log).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Clear", command=self.clear_log).pack(side="right")
        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True, pady=(6, 0))
        self.log = tk.Text(inner, height=14, wrap="none", bg="#14201b", fg="#d8e6dd",
                           font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(inner, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        for k, c in TAG_COLORS.items():
            self.log.tag_configure(k, foreground=c if k != "gioca" else "#7fd7a0")
        for k, c in (("relay", "#7fb2ff"), ("multiboot", "#ffcc70"), ("sblocca", "#ff9a9a"), ("finto", "#d8a0ff")):
            self.log.tag_configure(k, foreground=c)
        self.log.tag_configure("grave", foreground="#ff8f8f", font=("Consolas", 9, "bold"))

    # --- settings ---------------------------------------------------------

    def load_config_into_fields(self):
        c = self.p.config
        self.role.set(c["ruolo"])
        self.v_relay.set(str(c["relay"]))
        self.v_room.set(str(c["stanza"]))
        self.v_timing.set(str(c["timing"]))
        self.v_peer.set(str(c.get("peer") or ""))
        self.v_cable.set(c["cavo"])
        self.dirty = False

    def role_changed(self):
        if self.role.get() == "ospite" and not self.v_relay.get().startswith("127.0.0.1:"):
            port = self.v_relay.get().split(":")[-1] if ":" in self.v_relay.get() else "9000"
            self.v_relay.set("127.0.0.1:" + (port if port.isdigit() else "9000"))
        self.save()

    def save(self):
        def num(v, default):
            try:
                return int(str(v).strip())
            except ValueError:
                return default
        ok, msg = self.p.aggiorna_config({
            "ruolo": self.role.get(),
            "relay": self.v_relay.get().strip(),
            "stanza": num(self.v_room.get(), -1),
            "timing": num(self.v_timing.get(), -1),
            "peer": num(self.v_peer.get(), 0) if self.v_peer.get().strip() else 0,
            "cavo": self.v_cable.get() or "auto",
        })
        self.cfg_msg.set("Settings saved." if ok else msg)
        if ok:
            self.load_config_into_fields()
            self.root.after(3000, lambda: self.cfg_msg.set(""))
        return ok

    # --- actions ----------------------------------------------------------

    def pick_stub(self):
        path = filedialog.askopenfilename(title="Select mbstub.gba",
                                          filetypes=[("GBA program", "*.gba"), ("All files", "*.*")])
        if path:
            os.environ["MBSTUB_PATH"] = path
            try:
                with open(STUB_MEMO, "w", encoding="utf-8") as f:
                    f.write(path)
            except OSError:
                pass
            self.action_msg.set("")
        return path

    def start(self, what):
        if what in ("relay", "gioca", "finto") or what == "sblocca":
            if not self.save():
                self.action_msg.set(self.cfg_msg.get())
                return
        if what == "multiboot" and find_stub() is None:
            if not self.pick_stub():
                self.action_msg.set("mbstub.gba is needed to load the game into the GBA.")
                return
        stub = find_stub()
        if stub:
            os.environ["MBSTUB_PATH"] = stub
        ok, msg = self.p.avvia(what)
        self.action_msg.set("" if ok else msg)

    def toggle(self, what):
        if self.p.proc[what].vivo():
            ok, msg = self.p.ferma(what)
            self.action_msg.set("" if ok else msg)
        else:
            self.start(what)

    def close(self):
        try:
            self.p.ferma_tutto()
        finally:
            self.root.destroy()

    # --- log --------------------------------------------------------------

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def copy_log(self):
        text = self.p.testo_log()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.action_msg.set("")
        self.cfg_msg.set("Copied %d lines." % text.count("\n"))
        self.root.after(3000, lambda: self.cfg_msg.set(""))

    def save_log(self):
        path = filedialog.asksaveasfilename(defaultextension=".log", initialfile="gblink.log",
                                            filetypes=[("Log", "*.log"), ("Text", "*.txt")])
        if path:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(self.p.testo_log())
            except OSError as exc:
                messagebox.showerror("Save log", str(exc))

    def add_lines(self, lines):
        if not lines:
            return
        self.log.configure(state="normal")
        for r in lines:
            if self.only_game.get() and r["chi"] != "gioca":
                continue
            text = r["testo"]
            for prefix in ("[%s]" % r["chi"],):
                if text.startswith(prefix):
                    text = text[len(prefix):].lstrip()
            grave = any(w in text for w in ("!!!", "FAILED", "ERROR", "cannot", "NO EMULATOR"))
            self.log.insert("end", "[%s] " % r["chi"], r["chi"])
            self.log.insert("end", text + "\n", "grave" if grave else "")
        lines_now = int(self.log.index("end-1c").split(".")[0])
        if lines_now > 1500:
            self.log.delete("1.0", "%d.0" % (lines_now - 1500))
        self.log.configure(state="disabled")
        if self.follow.get():
            self.log.see("end")

    # --- refresh ------------------------------------------------------------

    def tick(self):
        try:
            s = self.p.stato(self.seq)
            self.seq = s["seq"]
            self.paint(s)
            self.add_lines(s["righe"])
        except Exception as exc:  # never let a paint error stop the refresh
            self.action_msg.set("display error: %s" % exc)
        self.root.after(400, self.tick)

    def paint(self, s):
        sy = s["sintesi"]
        procs = s["processi"]
        if sy["mappa"]:
            self.st_gba.set("Your GBA: in game (map %s)" % sy["mappa"])
        elif sy["caricato"]:
            self.st_gba.set("Your GBA: program loaded")
        else:
            self.st_gba.set("Your GBA: stopped")
        self.st_friend.set("Friend: connected" if sy["amico"] else "Friend: waiting")
        rate = sy["parole_s"] if sy["parole_s"] else "-"
        self.st_chan.set("Channel: %s words/s | sent %d | received %d" % (rate, sy["inviati"], sy["ricevuti"]))
        club = sy.get("club")
        self.st_club.set(("Cable Club: " + str(club).upper()) if club else "")
        if sy["avviso"]:
            self.warn.set("!  " + sy["avviso"])
            self.warn_label.pack(fill="x", padx=10, after=self.root.winfo_children()[0])
        else:
            self.warn_label.pack_forget()
        for name, b in self.buttons.items():
            b.configure(text="Stop" if procs[name]["vivo"] else "Start")
        self.btn_finto.configure(text="Stop the fake partner" if procs["finto"]["vivo"]
                                 else "Test alone (fake partner)")
        if not self.dirty and s["config"] != self.p.config:
            self.load_config_into_fields()


def main():
    if sys.platform == "win32" and getattr(sys, "frozen", False):
        try:   # hide the black console window: this is a window app
            import ctypes
            hwnd = ctypes.windll.kernel32.GetConsoleWindow()
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 0)
        except Exception:
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
