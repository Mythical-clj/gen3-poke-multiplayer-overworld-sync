"""gblink.exe: one small window (no Python needed) + a live map in your browser.

The engine is pannello.py (it starts relay.py, mb_multi.py, client.py,
usb_link.py as child processes). When the exe is started as
`gblink.exe -u script.py args` it runs that script instead of opening the
window: that is how the window starts its children.
"""
import os
import re
import runpy
import sys


def _run_child():
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

# Settings, logs and live positions live in the user's folder (a onefile exe
# unpacks to a temporary folder that disappears when it closes). Children
# inherit these variables, so the client writes the positions where the map
# server reads them.
DATA = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "gblink")
os.makedirs(os.path.join(DATA, "posizioni"), exist_ok=True)
os.environ.setdefault("PANNELLO_CONFIG", os.path.join(DATA, "config.json"))
os.environ.setdefault("PANNELLO_LOG", os.path.join(DATA, "log"))
os.environ["GBLINK_POSIZIONI"] = os.path.join(DATA, "posizioni")
MEMO_STUB = os.path.join(DATA, "mbstub-path.txt")
MEMO_MAP = os.path.join(DATA, "mappa-path.txt")

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

if False:  # never runs: lets PyInstaller see what the helper scripts import
    import argparse, base64, hashlib, heapq, json, queue, random, re, select  # noqa
    import selectors, socket, ssl, struct, subprocess, tempfile, threading  # noqa
    import time, urllib.error, urllib.parse, urllib.request, collections  # noqa
    import http.server, webbrowser  # noqa
    import usb.core, usb.util, usb.backend.libusb1, libusb_package  # noqa
    import protocol, usb_link, ws_link, club_link, club_mgba, finto_club  # noqa

import threading
import tkinter as tk
import webbrowser
from http.server import ThreadingHTTPServer
from tkinter import filedialog, messagebox, ttk

import pannello

RED = "#b3392f"
EXE_DIR = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))


def first_existing(cands, leaf=None):
    for c in cands:
        if c and os.path.exists(os.path.join(c, leaf) if leaf else c):
            return c
    return None


def memo(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def find_stub():
    return first_existing([os.environ.get("MBSTUB_PATH"), memo(MEMO_STUB),
                           os.path.join(EXE_DIR, "mbstub.gba"),
                           os.path.join(EXE_DIR, "net", "mbstub.gba"),
                           os.path.join(DATA, "mbstub.gba"), os.path.join(BASE, "mbstub.gba")])


def find_map_dir():
    return first_existing([os.environ.get("GBLINK_MAPPA"), memo(MEMO_MAP),
                           os.path.join(EXE_DIR, "mappa"), os.path.join(EXE_DIR, "net", "mappa"),
                           os.path.join(DATA, "mappa"), os.path.join(BASE, "mappa")], "dati.json")


# The window log shows only these by default; "Show details" shows everything
# (Copy/Save always export the full log).
IMPORTANT = re.compile(
    r"connect|club|battle|trade|friend|room|ready|done|loaded|warning|"
    r"stuck|lost|closed|timeout|joined|left", re.I)


class App:
    def __init__(self, root):
        self.root = root
        self.p = pannello.Pannello()
        self.seq = 0
        self.dirty = False
        self.play_stage = None      # "load" while the game is being sent to the GBA
        self.load_tries = 0
        self.httpd = None
        self.map_port = None
        root.title("gblink")
        root.geometry("720x600")
        root.minsize(620, 520)
        root.protocol("WM_DELETE_WINDOW", self.close)
        try:
            ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
        except tk.TclError:
            pass

        top = ttk.Frame(root, padding=(12, 10))
        top.pack(fill="x")
        self.status = tk.StringVar(value="GBA: stopped   |   Friend: waiting")
        ttk.Label(top, textvariable=self.status, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.warn = tk.StringVar(value="")
        self.warn_label = tk.Label(top, textvariable=self.warn, fg="white", bg=RED, anchor="w",
                                   padx=8, pady=4, wraplength=680, justify="left")

        big = ttk.Frame(root, padding=(12, 0))
        big.pack(fill="x")
        self.btn_play = ttk.Button(big, text="Play", command=self.toggle_play)
        self.btn_map = ttk.Button(big, text="Live map", command=self.open_map)
        for b in (self.btn_play, self.btn_map):
            b.pack(side="left", expand=True, fill="x", padx=4, ipady=10)
        self.msg = tk.StringVar(value="")
        ttk.Label(root, textvariable=self.msg, foreground=RED, wraplength=680).pack(anchor="w", padx=16, pady=(6, 0))

        sett = ttk.Frame(root, padding=(12, 8))
        sett.pack(fill="x")
        self.v_relay = tk.StringVar()
        self.v_room = tk.StringVar()
        self.v_host = tk.BooleanVar()
        self.v_timing = tk.StringVar()
        self.v_peer = tk.StringVar()
        self.v_cable = tk.StringVar()
        ttk.Label(sett, text="Relay").grid(row=0, column=0, sticky="w")
        ttk.Entry(sett, textvariable=self.v_relay, width=34).grid(row=0, column=1, padx=6)
        ttk.Label(sett, text="Room").grid(row=0, column=2, sticky="w")
        ttk.Entry(sett, textvariable=self.v_room, width=8).grid(row=0, column=3, padx=6)
        ttk.Checkbutton(sett, text="I host the relay", variable=self.v_host,
                        command=self.host_changed).grid(row=0, column=4, padx=6)
        for v in (self.v_relay, self.v_room, self.v_timing, self.v_peer, self.v_cable):
            v.trace_add("write", lambda *a: setattr(self, "dirty", True))

        self.adv_open = tk.BooleanVar(value=False)
        self.adv_check = ttk.Checkbutton(root, text="More options", variable=self.adv_open, command=self.toggle_adv)
        self.adv_check.pack(anchor="w", padx=16)
        self.adv = ttk.Frame(root, padding=(12, 4))
        row = ttk.Frame(self.adv)
        row.pack(fill="x")
        for i, (label, var, w) in enumerate((("Timing", self.v_timing, 7), ("Peer (0 = auto)", self.v_peer, 6))):
            ttk.Label(row, text=label).pack(side="left", padx=(0, 4))
            ttk.Entry(row, textvariable=var, width=w).pack(side="left", padx=(0, 12))
        ttk.Label(row, text="Cable").pack(side="left", padx=(0, 4))
        ttk.Combobox(row, textvariable=self.v_cable, values=("auto", "gba", "gbc"), state="readonly",
                     width=6).pack(side="left")
        row2 = ttk.Frame(self.adv)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="Unjam adapter", command=lambda: self.start("sblocca")).pack(side="left", padx=(0, 6))
        self.btn_load = ttk.Button(row2, text="Load game only", command=lambda: self.toggle("multiboot"))
        self.btn_load.pack(side="left", padx=(0, 6))
        self.btn_finto = ttk.Button(row2, text="Test alone (fake partner)", command=lambda: self.toggle("finto"))
        self.btn_finto.pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Choose mbstub.gba...", command=self.pick_stub).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="Choose map folder...", command=self.pick_map).pack(side="left")

        box = ttk.Frame(root, padding=(12, 6))
        box.pack(fill="both", expand=True)
        bar = ttk.Frame(box)
        bar.pack(fill="x")
        ttk.Label(bar, text="Log").pack(side="left")
        self.detail = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="Show details", variable=self.detail).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Copy", command=self.copy_log).pack(side="right", padx=(6, 0))
        ttk.Button(bar, text="Save...", command=self.save_log).pack(side="right", padx=(6, 0))
        inner = ttk.Frame(box)
        inner.pack(fill="both", expand=True, pady=(4, 0))
        self.log = tk.Text(inner, height=10, wrap="none", bg="#14201b", fg="#d8e6dd",
                           font=("Consolas", 9), state="disabled")
        sb = ttk.Scrollbar(inner, command=self.log.yview)
        self.log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("bad", foreground="#ff8f8f")

        self.load_fields()
        self.tick()

    # --- settings -------------------------------------------------------

    def toggle_adv(self):
        if self.adv_open.get():
            self.adv.pack(fill="x", after=self.adv_check)
        else:
            self.adv.pack_forget()

    def load_fields(self):
        c = self.p.config
        self.v_relay.set(str(c["relay"]))
        self.v_room.set(str(c["stanza"]))
        self.v_host.set(c["ruolo"] == "ospite")
        self.v_timing.set(str(c["timing"]))
        self.v_peer.set(str(c.get("peer") or ""))
        self.v_cable.set(c["cavo"])
        self.dirty = False

    def host_changed(self):
        if self.v_host.get() and not self.v_relay.get().startswith("127.0.0.1:"):
            port = self.v_relay.get().rsplit(":", 1)[-1] if ":" in self.v_relay.get() else "9000"
            self.v_relay.set("127.0.0.1:" + (port if port.isdigit() else "9000"))

    def save(self):
        def num(v, default):
            try:
                return int(str(v).strip())
            except ValueError:
                return default
        ok, text = self.p.aggiorna_config({
            "ruolo": "ospite" if self.v_host.get() else "amico",
            "relay": self.v_relay.get().strip(),
            "stanza": num(self.v_room.get(), -1),
            "timing": num(self.v_timing.get(), -1),
            "peer": num(self.v_peer.get(), 0) if self.v_peer.get().strip() else 0,
            "cavo": self.v_cable.get() or "auto",
        })
        if ok:
            self.load_fields()
            self.msg.set("")
        else:
            self.msg.set(text)
        return ok

    # --- actions --------------------------------------------------------

    def pick_stub(self):
        path = filedialog.askopenfilename(title="Select mbstub.gba",
                                          filetypes=[("GBA program", "*.gba"), ("All files", "*.*")])
        if path:
            os.environ["MBSTUB_PATH"] = path
            try:
                with open(MEMO_STUB, "w", encoding="utf-8") as f:
                    f.write(path)
            except OSError:
                pass
        return path

    def pick_map(self):
        path = filedialog.askdirectory(title="Select the map folder (the one with dati.json)")
        if path and os.path.exists(os.path.join(path, "dati.json")):
            os.environ["GBLINK_MAPPA"] = path
            pannello.MAPPA_DIR = path
            try:
                with open(MEMO_MAP, "w", encoding="utf-8") as f:
                    f.write(path)
            except OSError:
                pass
            return path
        if path:
            self.msg.set("That folder has no dati.json.")
        return None

    def start(self, what):
        if not self.save():
            return False
        if what == "multiboot":
            if find_stub() is None:
                self.msg.set("mbstub.gba was not found. Put it next to gblink.exe, "
                             "or choose it under More options.")
                return False
            os.environ["MBSTUB_PATH"] = find_stub()
        ok, text = self.p.avvia(what)
        self.msg.set("" if ok else text)
        return ok

    def toggle(self, what):
        if self.p.proc[what].vivo():
            self.p.ferma(what)
        else:
            self.start(what)

    def toggle_play(self):
        # Play = send the game to the GBA if one is waiting for it, then connect.
        # With the game already on a cartridge nothing answers the loader, and
        # it goes straight to the link.
        if self.play_stage or self.p.proc["gioca"].vivo() or self.p.proc["multiboot"].vivo():
            self.play_stage = None
            self.p.ferma("multiboot")
            self.p.ferma("gioca")
            return
        if not self.save():
            return
        if self.v_host.get() and not self.p.proc["relay"].vivo():
            self.p.avvia("relay")     # the host's relay starts by itself
        self.load_tries = 0
        if find_stub() is not None and self.start("multiboot"):
            self.play_stage = "load"
        else:
            self.start("gioca")

    def go_play(self):
        if self.play_stage == "boot":
            self.play_stage = None
            self.start("gioca")

    def advance_play(self, sy, procs):
        """Called every refresh: moves from loading the game to playing."""
        if self.play_stage != "load" or procs["multiboot"]["vivo"]:
            return
        if sy["caricato"]:
            # The adapter restarts itself after the transfer: give it a moment.
            self.play_stage = "boot"
            self.root.after(2500, self.go_play)
        elif sy["mb_visto"] and self.load_tries < 2:
            self.load_tries += 1          # a GBA was waiting: the transfer slipped, try again
            self.start("multiboot")
        else:
            self.play_stage = None        # nobody was waiting: the game is already running
            self.p.sintesi["avviso"] = None
            self.start("gioca")

    def open_map(self):
        mdir = find_map_dir()
        if mdir is None:
            messagebox.showinfo("Live map",
                                "The map pictures were not found (they are normally built into gblink.exe).\n\n"
                                "Pick a 'mappa' folder (the one with dati.json), "
                                "or put it next to gblink.exe.")
            mdir = self.pick_map()
            if mdir is None:
                return
        os.environ["GBLINK_MAPPA"] = mdir
        pannello.MAPPA_DIR = mdir
        if self.httpd is None:
            pannello.Handler.pannello = self.p
            for port in range(7411, 7431):
                try:
                    self.httpd = ThreadingHTTPServer(("127.0.0.1", port), pannello.Handler)
                    self.map_port = port
                    break
                except OSError:
                    continue
            if self.httpd is None:
                self.msg.set("Could not open a local port for the map.")
                return
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        webbrowser.open("http://127.0.0.1:%d/mappa.html" % self.map_port)

    def close(self):
        try:
            self.p.ferma_tutto()
            if self.httpd is not None:
                self.httpd.shutdown()
        finally:
            self.root.destroy()

    # --- log ------------------------------------------------------------

    def copy_log(self):
        text = self.p.testo_log()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.msg.set("")

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
            text = r["testo"]
            prefix = "[%s]" % r["chi"]
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip()
            bad = any(w in text for w in ("!!!", "FAILED", "ERROR", "cannot", "NO EMULATOR"))
            if not (bad or self.detail.get() or IMPORTANT.search(text)):
                continue
            self.log.insert("end", "[%s] %s\n" % (r["chi"], text), "bad" if bad else "")
        n = int(self.log.index("end-1c").split(".")[0])
        if n > 1500:
            self.log.delete("1.0", "%d.0" % (n - 1500))
        self.log.configure(state="disabled")
        self.log.see("end")

    # --- refresh --------------------------------------------------------

    def tick(self):
        try:
            s = self.p.stato(self.seq)
            self.seq = s["seq"]
            self.paint(s)
            self.add_lines(s["righe"])
        except Exception as exc:
            self.msg.set("display error: %s" % exc)
        self.root.after(400, self.tick)

    def paint(self, s):
        sy, procs = s["sintesi"], s["processi"]
        if sy["mappa"]:
            gba = "GBA: in game (map %s)" % sy["mappa"]
        elif self.play_stage in ("load", "boot"):
            gba = "GBA: loading the game..."
        elif procs["gioca"]["vivo"]:
            gba = "GBA: connecting..."
        else:
            gba = "GBA: stopped"
        parts = [gba, "Friend: connected" if sy["amico"] else "Friend: waiting"]
        if sy["parole_s"]:
            parts.append("%s words/s" % sy["parole_s"])
        if sy.get("club"):
            parts.append("Cable Club: %s" % str(sy["club"]).upper())
        self.status.set("   |   ".join(parts))
        if sy["avviso"]:
            self.warn.set(sy["avviso"])
            self.warn_label.pack(fill="x", pady=(6, 0))
        else:
            self.warn_label.pack_forget()
        self.btn_load.configure(text="Stop loading" if procs["multiboot"]["vivo"] else "Load game only")
        busy = self.play_stage or procs["gioca"]["vivo"] or procs["multiboot"]["vivo"]
        self.btn_play.configure(text="Stop" if busy else "Play")
        self.advance_play(sy, procs)
        self.btn_finto.configure(text="Stop the fake partner" if procs["finto"]["vivo"]
                                 else "Test alone (fake partner)")


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
