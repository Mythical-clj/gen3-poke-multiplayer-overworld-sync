"""Entry point of the Windows .exe: opens the panel, and also runs the helper
scripts (client.py, relay.py, ...) that the panel starts as child processes
with `<this exe> -u <script> <args>`."""
import os
import runpy
import sys

BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

if False:  # never runs: lets PyInstaller see the modules the scripts import
    import argparse, base64, hashlib, heapq, json, queue, random, re, select  # noqa
    import selectors, socket, ssl, struct, subprocess, tempfile, threading  # noqa
    import time, urllib.error, urllib.parse, urllib.request, webbrowser  # noqa
    import http.server, collections  # noqa
    import usb.core, usb.util, usb.backend.libusb1, libusb_package  # noqa
    import protocol, usb_link, ws_link, club_link, club_mgba, finto_club  # noqa


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "-u" and args[1].endswith(".py"):
        script = args[1]
        sys.argv = args[1:]
        runpy.run_path(script, run_name="__main__")
        return
    sys.argv = [os.path.join(BASE, "pannello.py")]
    runpy.run_path(sys.argv[0], run_name="__main__")


if __name__ == "__main__":
    main()
