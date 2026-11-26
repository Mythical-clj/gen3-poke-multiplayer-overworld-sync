#!/usr/bin/env python3
"""
pannello.py - la REGIA: un pannello web locale al posto dei quattro .bat.

PERCHE' ESISTE (2026-08-09, richiesta di Lain). Fino a ieri per giocare
servivano: avvia-relay.bat, 1-multiboot.bat, 2-gioca.bat, 3-sblocca.bat -
e sapere quale, in che ordine, e quando. Il pannello li racchiude in una
pagina sola: si apre, si preme, si gioca. Sotto NON cambia niente: fa girare
gli STESSI script (relay.py, mb_multi.py, client.py, usb_link.py) come
sottoprocessi, con gli stessi argomenti a verbale. Se il pannello si rompe,
i .bat restano e funzionano.

Scelte che contano:

- SOLO LIBRERIA STANDARD. Niente Flask, niente aiohttp: l'amico ha un Python
  embeddable e ogni dipendenza in piu' e' un modo di fallire sul suo PC.
  http.server basta per un pannello locale.
- SOLO 127.0.0.1. Questo processo AVVIA PROGRAMMI: non deve essere
  raggiungibile dalla rete. Il bind e' esplicito sull'interfaccia di loopback
  e non e' configurabile apposta.
- I FIGLI GIRANO CON -u. Senza, Python bufferizza lo stdout quando non e' un
  terminale e il log del pannello arriverebbe a blocchi da 8 KB, cioe' in
  ritardo di minuti proprio quando serve guardarlo.
- MUTUA ESCLUSIONE SULL'USB. multiboot e partita vogliono tutti e due
  l'adattatore: il pannello rifiuta di avviarne uno mentre l'altro gira,
  invece di lasciare che si contendano il device e fallisca il secondo con un
  errore incomprensibile.

Uso:
    python pannello.py                 # apre il browser da solo
    python pannello.py --porta 7411 --niente-browser
"""

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

QUI = os.path.dirname(os.path.abspath(__file__))
# La variabile d'ambiente serve ai test: senza, provare il pannello
# SOVRASCRIVE la configurazione vera dell'utente (successo il 2026-08-09: il
# test lascio' dentro una porta di relay casuale).
CONFIG_PATH = os.environ.get("PANNELLO_CONFIG") or \
    os.path.join(QUI, "pannello.config.json")
HTML_PATH = os.path.join(QUI, "pannello.html")

# Quante righe di log tenere in memoria PER LA PAGINA (il ticker live).
# Il registro COMPLETO della sessione sta su file (vedi LOG_DIR): la copia e
# il download passano da li', quindi il tetto in RAM non taglia piu' niente
# a chi fa debug — taglia solo quello che il browser ridisegna.
MAX_RIGHE = 4000

# Il registro completo su disco: un file per sessione, niente tetto.
# PANNELLO_LOG serve ai test (come PANNELLO_CONFIG): senza, i test
# sporcherebbero la cartella dei log veri.
LOG_DIR = os.environ.get("PANNELLO_LOG") or os.path.join(QUI, "log")

CONFIG_DEFAULT = {
    "ruolo": "ospite",        # "ospite" = ospito io il relay | "amico" = mi collego
    "relay": "127.0.0.1:9000",
    "stanza": 4242,
    "timing": 7400,
    "peer": 0,                # 0 = automatico (ospite->1, amico->2); in 3+ va scelto a mano
    "cavo": "auto",
    "porta_relay": 9000,
}

RE_RELAY = re.compile(r"^[A-Za-z0-9.\-]{1,255}:\d{1,5}$")
# Dal 2026-08-25 il relay si raggiunge anche via WebSocket (ws:// o
# wss://): e' la strada del browser, sulla 443, ed e' l'UNICA che passa
# verso la VPS - la 9000/udp e' filtrata dalla Security List della VCN.
RE_RELAY_WS = re.compile(r"^wss?://[A-Za-z0-9.\-]{1,255}(:\d{1,5})?(/[^\s]*)?$")


def python_exe():
    """L'interprete con cui lanciare i figli: lo stesso che gira ora.

    sys.executable e' l'unica risposta giusta - su questo PC 'python' sul PATH
    e' il segnaposto muto del Microsoft Store (exit 9009, zero output), che il
    2026-08-02 e' costato tre finestre che si aprivano e chiudevano in
    silenzio."""
    return sys.executable


class Processo:
    """Un sottoprocesso sorvegliato: lo avvia, ne legge lo stdout riga per
    riga in un thread, e sa dire se e' ancora vivo."""

    def __init__(self, nome, pannello):
        self.nome = nome
        self.pannello = pannello
        self.proc = None
        self.lettore = None
        self.avviato = None
        self.uscita = None

    def vivo(self):
        return self.proc is not None and self.proc.poll() is None

    def avvia(self, argv):
        if self.vivo():
            return False, "already running"
        self.uscita = None
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=QUI,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=1,
                universal_newlines=True,
                encoding="utf-8",
                errors="replace",
                env=dict(os.environ, PYTHONIOENCODING="utf-8",
                         PYTHONUNBUFFERED="1"),
                # no black console window per child when started from the GUI
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0),
            )
        except Exception as exc:
            self.pannello.riga(self.nome, "START FAILED: %s" % exc)
            return False, str(exc)

        self.avviato = time.time()
        self.pannello.riga(self.nome, "started: %s" % " ".join(argv[1:]))
        self.lettore = threading.Thread(target=self._leggi, daemon=True,
                                        name="pannello-%s" % self.nome)
        self.lettore.start()
        return True, "ok"

    def _leggi(self):
        proc = self.proc
        try:
            for riga in proc.stdout:
                self.pannello.riga(self.nome, riga.rstrip("\r\n"))
        except Exception:
            pass
        code = proc.wait()
        self.uscita = code
        self.pannello.riga(self.nome, "--- finished (exit code %s) ---" % code)

    def ferma(self):
        if not self.vivo():
            return False
        self.pannello.riga(self.nome, "stop requested by the panel")
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        except Exception as exc:
            self.pannello.riga(self.nome, "problem while stopping: %s" % exc)
        return True


class Pannello:

    def __init__(self):
        self.lock = threading.Lock()
        self.righe = []           # (seq, quando, chi, testo)
        self.seq = 0
        self.config = dict(CONFIG_DEFAULT)
        self.carica_config()

        # Il registro completo della sessione, su disco e senza tetto: la
        # copia e il download devono restituire TUTTO, non le ultime 4000
        # righe. Se il file non si apre il pannello vive lo stesso (RAM sola).
        self.log_path = None
        self._log_fh = None
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            nome = "pannello-%s.log" % time.strftime("%Y%m%d-%H%M%S")
            self.log_path = os.path.join(LOG_DIR, nome)
            self._log_fh = open(self.log_path, "a", encoding="utf-8")
        except Exception as exc:
            print("log file not available: %s" % exc)

        self.proc = {
            "relay": Processo("relay", self),
            "multiboot": Processo("multiboot", self),
            "gioca": Processo("gioca", self),
            "sblocca": Processo("sblocca", self),
            # Il partner finto (procedura V): recita l'amico per provare il
            # Cable Club DA SOLI. Solo rete, niente USB: convive con la
            # partita, ed e' proprio il punto - gira INSIEME a "gioca".
            "finto": Processo("finto", self),
        }

        # La sintesi mostrata in alto: si ricava LEGGENDO le righe di log dei
        # figli, non duplicando la logica. E' il motivo per cui i formati di
        # quelle righe sono un contratto - vedi _annusa().
        self.sintesi = self._sintesi_vuota()

    def _sintesi_vuota(self):
        return {
            "mappa": None,
            "inviati": 0,
            "ricevuti": 0,
            "parole_s": None,
            "amico": False,
            "club": None,
            "caricato": False,
            "avviso": None,
        }

    # --- la mappa live ----------------------------------------------------

    def posizioni(self):
        """Fonde i posizioni-<peer>.json scritti dai client (uno per client:
        in emulatore ce ne sono due sullo stesso PC). Ogni file e' la vista di
        UN client: il suo giocatore (`io`) e gli amici che vede."""
        viste = []
        cartella = os.path.join(QUI, "posizioni")
        try:
            nomi = sorted(os.listdir(cartella))
        except OSError:
            nomi = []
        adesso = time.time()
        for nome in nomi:
            if not re.fullmatch(r"posizioni-\d+\.json", nome):
                continue
            try:
                with open(os.path.join(cartella, nome), "r", encoding="utf-8") as f:
                    v = json.load(f)
            except (OSError, ValueError):
                continue
            # Il client vivo riscrive il file ogni 2 s: una vista piu' vecchia
            # di 15 s e' di un client chiuso (o di un test) e non si mostra.
            if adesso - float(v.get("t", 0)) > 15:
                continue
            viste.append(v)
        return {"t": time.time(), "viste": viste,
                "mappa_pronta": os.path.exists(os.path.join(QUI, "mappa", "dati.json"))}

    # --- configurazione ---------------------------------------------------

    def carica_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                salvata = json.load(f)
            for k in CONFIG_DEFAULT:
                if k in salvata:
                    self.config[k] = salvata[k]
        except FileNotFoundError:
            pass
        except Exception as exc:
            print("[panel] unreadable config (%s): using the defaults"
                  % exc)

    def salva_config(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def aggiorna_config(self, nuova):
        """Valida e applica. Ritorna (ok, messaggio)."""
        c = dict(self.config)

        ruolo = nuova.get("ruolo", c["ruolo"])
        if ruolo not in ("ospite", "amico"):
            return False, "unknown role"
        c["ruolo"] = ruolo

        relay = str(nuova.get("relay", c["relay"])).strip()
        relay_ws = bool(RE_RELAY_WS.match(relay))
        if not relay_ws:
            if not RE_RELAY.match(relay):
                return False, ("invalid relay address: it must be "
                               "'host:port' (for example 93.42.10.20:9000) "
                               "or a WebSocket address "
                               "(wss://gbcatrade.wired-ariel.it/ws)")
            host, _, porta = relay.partition(":")
            if not (0 < int(porta) < 65536):
                return False, "relay port out of range"
        c["relay"] = relay

        try:
            stanza = int(nuova.get("stanza", c["stanza"]))
        except (TypeError, ValueError):
            return False, "the room must be a number"
        if not (1 <= stanza <= 65535):
            return False, "the room must be between 1 and 65535"
        c["stanza"] = stanza

        try:
            timing = int(nuova.get("timing", c["timing"]))
        except (TypeError, ValueError):
            return False, "invalid timing"
        if not (1000 <= timing <= 60000):
            return False, "timing out of range (1000-60000)"
        c["timing"] = timing

        try:
            peer = int(nuova.get("peer", c.get("peer", 0)))
        except (TypeError, ValueError):
            return False, "the peer must be a number"
        if not (0 <= peer <= 65534):
            return False, "the peer goes from 1 to 65534 (0 = automatic)"
        c["peer"] = peer

        cavo = nuova.get("cavo", c["cavo"])
        if cavo not in ("auto", "gba", "gbc"):
            return False, "unknown cable type"
        c["cavo"] = cavo

        if ruolo == "ospite":
            # Chi ospita fa girare il relay sul proprio PC: il client si
            # collega in locale, e la porta e' quella del relay. Con un
            # indirizzo WebSocket non c'e' niente da ospitare - il relay sta
            # sulla macchina in rete - e il ruolo torna "amico" da solo,
            # invece di lasciare un pulsante che accenderebbe un relay che
            # nessuno userebbe.
            if relay_ws:
                c["ruolo"] = "amico"
            else:
                c["porta_relay"] = int(c["relay"].partition(":")[2])

        self.config = c
        self.salva_config()
        return True, "saved"

    # --- log ---------------------------------------------------------------

    def riga(self, chi, testo):
        with self.lock:
            self.seq += 1
            adesso = time.time()
            self.righe.append({"n": self.seq, "t": adesso,
                               "chi": chi, "testo": testo})
            if len(self.righe) > MAX_RIGHE:
                del self.righe[:len(self.righe) - MAX_RIGHE]
            # getattr: i test costruiscono Pannello con __new__, senza file
            if getattr(self, "_log_fh", None) is not None:
                try:
                    stampa = time.strftime("%H:%M:%S", time.localtime(adesso))
                    self._log_fh.write("%s [%s] %s\n" % (stampa, chi, testo))
                    self._log_fh.flush()
                except Exception:
                    self._log_fh = None   # disco pieno o file sparito: non si insiste
            self._annusa(chi, testo)

    # I formati letti qui sono un CONTRATTO con client.py e mb_multi.py: se
    # cambiano quelle stringhe, la sintesi in cima al pannello smette di
    # aggiornarsi senza nessun errore. Sono tenuti pochi e grossolani apposta.
    RE_MAPPA = re.compile(r"(?:io su mappa|me on map) (\d+)\.(\d+)")
    RE_CONTI = re.compile(r"(?:inviati|sent) (\d+) \| (?:ricevuti|received) (\d+)")
    RE_PAROLE = re.compile(r"rx \d+ frames?, ([\d~?]+) (?:parole|words)/s")

    def _annusa(self, chi, testo):
        s = self.sintesi

        if chi == "gioca":
            m = self.RE_MAPPA.search(testo)
            if m:
                s["mappa"] = "%s.%s" % (m.group(1), m.group(2))
            m = self.RE_CONTI.search(testo)
            if m:
                s["inviati"] = int(m.group(1))
                s["ricevuti"] = int(m.group(2))
                s["amico"] = int(m.group(2)) > 0
            m = self.RE_PAROLE.search(testo)
            if m:
                s["parole_s"] = m.group(1)

            if "[club ]" in testo:
                if "si ricammina" in testo or "sessione chiusa" in testo or "session closed" in testo or "peer left the club" in testo or "left the club" in testo:
                    s["club"] = None
                elif "entrato al Cable Club" in testo or "entered the Cable Club" in testo:
                    s["club"] = "in corso"
                elif "ruolo:" in testo and "MASTER" in testo:
                    s["club"] = "master"
                elif "ruolo:" in testo and "SLAVE" in testo:
                    s["club"] = "slave"

            # I guai che il pannello deve gridare, non nascondere.
            if "CANALE CON IL GBA FERMO" in testo or "GBA CHANNEL STUCK" in testo:
                s["avviso"] = ("The channel to the GBA has stalled. Press "
                               "UNJAM ADAPTER: do NOT turn the GBA off.")
            elif "ha chiuso la porta seriale" in testo or "closed the serial port" in testo:
                s["avviso"] = ("You are outside the game world (menu, house, "
                               "battle): your friend sees you standing still. "
                               "Go back outdoors.")
            elif "NESSUN EMULATORE COLLEGATO" in testo or "NO EMULATOR CONNECTED" in testo:
                s["avviso"] = "No emulator connected: load the script in mGBA."
            elif "non ha ancora detto su che mappa" in testo or "has not said which map" in testo:
                s["avviso"] = ("The GBA is not saying where you are: are you in the "
                               "game, outdoors? On the title screen the program stays quiet.")
            elif s["avviso"] and (("inviati" in testo or " | sent " in testo) and s["mappa"]):
                s["avviso"] = None

        elif chi == "multiboot":
            if "DONE!" in testo:
                s["caricato"] = True
                s["avviso"] = None
            if "nessuna risposta" in testo or "no response" in testo:
                s["avviso"] = ("The GBA is not answering: cartridge slot EMPTY and "
                               "console turned on AFTER the cable? Try again, "
                               "one failure in three is normal.")

    # --- avvio dei pezzi ---------------------------------------------------

    def usb_occupato(self):
        for nome in ("multiboot", "gioca", "sblocca"):
            if self.proc[nome].vivo():
                return nome
        return None

    def avvia(self, cosa):
        c = self.config
        py = python_exe()

        if cosa == "relay":
            if c["ruolo"] != "ospite":
                return False, ("the relay is started by the HOST: you connect "
                               "to your friend's")
            porta = int(c["relay"].partition(":")[2])
            return self.proc["relay"].avvia(
                [py, "-u", os.path.join(QUI, "relay.py"),
                 "--port", str(porta), "--verbose"])

        if cosa in ("multiboot", "gioca", "sblocca"):
            occupato = self.usb_occupato()
            if occupato and occupato != cosa:
                return False, ("the adapter is already in use by '%s': "
                               "stop it first" % occupato)

        if cosa == "multiboot":
            stub = os.environ.get("MBSTUB_PATH") or ""
            if not os.path.exists(stub):
                stub = os.path.join(QUI, "mbstub.gba")
            if not os.path.exists(stub):
                stub = os.path.join(QUI, "..", "hw", "mbstub", "build",
                                    "mbstub.gba")
            if not os.path.exists(stub):
                return False, ("mbstub.gba not found: it is the program that "
                               "gets loaded into the GBA")
            self.sintesi["caricato"] = False
            return self.proc["multiboot"].avvia(
                [py, "-u", os.path.join(QUI, "mb_multi.py"),
                 os.path.abspath(stub)])

        if cosa == "gioca":
            # peer scelto a mano (partite a 3+ o miste GBA/emulatori/browser),
            # altrimenti l'automatico di sempre: ospite=1, amico=2
            peer = c.get("peer") or (1 if c["ruolo"] == "ospite" else 2)
            self.sintesi.update(self._sintesi_vuota())
            self.sintesi["caricato"] = True
            return self.proc["gioca"].avvia(
                [py, "-u", os.path.join(QUI, "client.py"),
                 "--transport", "usb",
                 "--relay", c["relay"],
                 "--room", str(c["stanza"]),
                 "--peer-id", str(peer),
                 "--usb-timing", str(c["timing"]),
                 "--usb-cable", c["cavo"]])

        if cosa == "sblocca":
            return self.proc["sblocca"].avvia(
                [py, "-u", os.path.join(QUI, "usb_link.py"), "--riavvia"])

        if cosa == "finto":
            # Il peer OPPOSTO a quello della partita: se qui si e' peer 1,
            # il finto fa il 2 (il ruolo per cui e' scritto: il master del
            # gioco che conduce). Non tocca l'USB: nessuna mutua esclusione.
            peer = 2 if c["ruolo"] == "ospite" else 1
            if c.get("peer") == peer:      # peer scelto a mano: il finto non deve collidere
                peer = peer + 2
            return self.proc["finto"].avvia(
                [py, "-u", os.path.join(QUI, "finto_club.py"),
                 "--relay", c["relay"],
                 "--room", str(c["stanza"]),
                 "--peer-id", str(peer)])

        return False, "I do not know what '%s' is" % cosa

    def ferma(self, cosa):
        p = self.proc.get(cosa)
        if p is None:
            return False, "I do not know what '%s' is" % cosa
        if not p.ferma():
            return False, "it was not running"
        return True, "stopped"

    def ferma_tutto(self):
        for p in self.proc.values():
            p.ferma()

    def testo_log(self):
        """TUTTO il registro della sessione, in testo semplice.

        La fonte e' il file su disco, che non ha tetto: e' l'unico posto dove
        una sessione lunga sta per intero (la RAM taglia a MAX_RIGHE, il
        browser ha solo cio' che ha ricevuto). Se il file non c'e' si ripiega
        sulla RAM: meglio 4000 righe che zero."""
        if getattr(self, "log_path", None) is not None:
            try:
                # flush prima di leggere: l'ultima riga deve esserci.
                with self.lock:
                    if getattr(self, "_log_fh", None) is not None:
                        self._log_fh.flush()
                with open(self.log_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception:
                pass
        with self.lock:
            righe = list(self.righe)
        fuori = []
        for r in righe:
            stampa = time.strftime("%H:%M:%S", time.localtime(r["t"]))
            fuori.append("%s [%s] %s" % (stampa, r["chi"], r["testo"]))
        return "\n".join(fuori) + "\n"

    # --- stato per il browser ---------------------------------------------

    def stato(self, da):
        with self.lock:
            nuove = [r for r in self.righe if r["n"] > da]
            processi = {n: {"vivo": p.vivo(), "uscita": p.uscita}
                        for n, p in self.proc.items()}
            return {
                "seq": self.seq,
                "righe": nuove[-500:],
                "processi": processi,
                "config": self.config,
                "sintesi": self.sintesi,
            }


class Handler(BaseHTTPRequestHandler):

    server_version = "PannelloGBA/1.0"
    pannello = None      # iniettato dal main

    def log_message(self, fmt, *args):
        pass             # il log HTTP non serve e sporcherebbe la console

    # -- utilita' -----------------------------------------------------------

    def _json(self, obj, code=200):
        corpo = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return {}
        if n <= 0 or n > 64 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    # -- rotte --------------------------------------------------------------

    def do_GET(self):
        percorso = self.path.split("?", 1)[0]

        if percorso in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    corpo = f.read()
            except FileNotFoundError:
                self.send_error(500, "pannello.html not found")
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)
            return

        if percorso == "/api/log":
            corpo = self.pannello.testo_log().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            # ?scarica=1: il browser salva un file invece di mostrare la pagina.
            if "scarica=1" in (self.path.split("?", 1) + [""])[1]:
                nome = "gen3-poke-multiplayer-%s.log" % time.strftime("%Y%m%d-%H%M%S")
                self.send_header("Content-Disposition",
                                 'attachment; filename="%s"' % nome)
            self.end_headers()
            self.wfile.write(corpo)
            return

        if percorso == "/api/tick":
            da = 0
            if "?" in self.path:
                for pezzo in self.path.split("?", 1)[1].split("&"):
                    k, _, v = pezzo.partition("=")
                    if k == "da":
                        try:
                            da = int(v)
                        except ValueError:
                            da = 0
            self._json(self.pannello.stato(da))
            return

        # LA MAPPA LIVE (2026-08-21): la pagina, i suoi dati (generati da
        # tools/gen_mappa.py dalla decomp) e le posizioni che i client
        # scrivono in posizioni-<peer>.json. Stessa disciplina delle
        # immagini: nomi piatti, estensioni fisse, niente path traversal.
        if percorso == "/mappa.html":
            strada = os.path.join(QUI, "mappa.html")
            if not os.path.exists(strada):
                self.send_error(404, "mappa.html not found")
                return
            with open(strada, "rb") as f:
                corpo = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(corpo)
            return

        if percorso == "/api/posizioni":
            self._json(self.pannello.posizioni())
            return

        m = re.fullmatch(r"/mappa/(dati\.json|nomi\.json|(?:img|icone|sprite|oe|item|front)/[A-Za-z0-9._-]+\.png"
                         r"|bw/[A-Za-z0-9._-]+\.gif)", percorso)
        if m:
            strada = os.path.join(QUI, "mappa", *m.group(1).split("/"))
            if not os.path.exists(strada):
                self.send_error(404, "map data not generated: tools\\gen_mappa.py")
                return
            with open(strada, "rb") as f:
                corpo = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8"
                             if strada.endswith(".json")
                             else ("image/gif" if strada.endswith(".gif") else "image/png"))
            self.send_header("Content-Length", str(len(corpo)))
            # dati.json si rigenera (gen_mappa.py) e va riletto: niente cache;
            # le immagini sono stabili e pesano: cache.
            self.send_header("Cache-Control", "no-store" if strada.endswith(".json") else "max-age=600")
            self.end_headers()
            self.wfile.write(corpo)
            return

        # I FONT del sito (2026-10-11): mappa.html li chiede come fonts/x.woff2.
        # Stanno in web/fonts/ accanto a net/ (nel repository); nei pacchetti
        # che non li portano la risposta e' 404 e la pagina ricade sui font di
        # sistema. Stessa disciplina delle immagini: un nome piatto e basta.
        if percorso.startswith("/fonts/") and percorso.count("/") == 2:
            nome = percorso[len("/fonts/"):]
            if re.fullmatch(r"[A-Za-z0-9._-]+\.woff2", nome or ""):
                for cartella in (os.path.join(QUI, "fonts"), os.path.join(QUI, "..", "web", "fonts")):
                    strada = os.path.join(cartella, nome)
                    if os.path.exists(strada):
                        with open(strada, "rb") as f:
                            corpo = f.read()
                        self.send_response(200)
                        self.send_header("Content-Type", "font/woff2")
                        self.send_header("Content-Length", str(len(corpo)))
                        self.send_header("Cache-Control", "max-age=86400")
                        self.end_headers()
                        self.wfile.write(corpo)
                        return

        # Le immagini della pagina (net/img/): il pannello sta su loopback ma
        # avvia programmi, quindi niente path traversal - solo un nome piatto
        # che finisce in .png, dentro net/img/.
        if percorso.startswith("/img/") and percorso.count("/") == 2:
            nome = percorso[len("/img/"):]
            if re.fullmatch(r"[A-Za-z0-9._-]+\.png", nome or ""):
                strada = os.path.join(QUI, "img", nome)
                if os.path.exists(strada):
                    with open(strada, "rb") as f:
                        corpo = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(corpo)))
                    self.send_header("Cache-Control", "max-age=3600")
                    self.end_headers()
                    self.wfile.write(corpo)
                    return

        self.send_error(404)

    def do_POST(self):
        percorso = self.path.split("?", 1)[0]
        dati = self._corpo()

        if percorso == "/api/config":
            ok, msg = self.pannello.aggiorna_config(dati)
            self._json({"ok": ok, "messaggio": msg,
                        "config": self.pannello.config})
            return

        if percorso == "/api/avvia":
            ok, msg = self.pannello.avvia(str(dati.get("cosa", "")))
            self._json({"ok": ok, "messaggio": msg})
            return

        if percorso == "/api/ferma":
            ok, msg = self.pannello.ferma(str(dati.get("cosa", "")))
            self._json({"ok": ok, "messaggio": msg})
            return

        self.send_error(404)


def main():
    ap = argparse.ArgumentParser(
        description="Local web panel for the overworld link")
    ap.add_argument("--porta", type=int, default=7411)
    ap.add_argument("--niente-browser", action="store_true")
    args = ap.parse_args()

    pannello = Pannello()
    Handler.pannello = pannello

    # SOLO loopback: questo processo avvia programmi, non deve essere
    # raggiungibile da fuori. Non e' configurabile apposta.
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", args.porta), Handler)
    except OSError as exc:
        raise SystemExit(
            "[panel] cannot open port %d (%s).\n"
            "[panel] The panel may already be open in another window: "
            "check your browser." % (args.porta, exc))

    url = "http://127.0.0.1:%d/" % args.porta
    print("=" * 62)
    print("  PANEL OPEN:  %s" % url)
    print("  Leave this window open. To close everything: Ctrl+C.")
    print("=" * 62, flush=True)

    if not args.niente_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[panel] shutting down: stopping everything I started...")
    finally:
        pannello.ferma_tutto()


if __name__ == "__main__":
    main()
