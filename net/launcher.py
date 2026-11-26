"""gblink.exe: a small launcher. It finds Python on this PC, makes sure the
two USB libraries are installed, and runs the panel (net/pannello.py).

It needs Python 3.8 or newer installed (python.org, tick "Add python.exe to
PATH"). Everything else comes from the files next to this exe."""
import os
import subprocess
import sys
import webbrowser

NEEDED = "import usb.core, usb.util, libusb_package"


def here():
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def find_net():
    base = here()
    for cand in (os.path.join(base, "net"), base, os.path.join(base, "..", "net")):
        if os.path.exists(os.path.join(cand, "pannello.py")):
            return os.path.abspath(cand)
    return None


def run_quiet(cmd, timeout=60):
    """Runs a command; returns (exit code, output) or (None, '') if it cannot start."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception:
        return None, ""


def find_python():
    for cmd in (["py", "-3"], ["python"], ["python3"]):
        code, out = run_quiet(cmd + ["-c", "import sys;print(sys.version_info[0], sys.version_info[1])"], 15)
        if code != 0:
            continue          # also skips the Microsoft Store "python" placeholder
        try:
            major, minor = [int(x) for x in out.split()[-2:]]
        except ValueError:
            continue
        if (major, minor) >= (3, 8):
            return cmd
    return None


def pause():
    try:
        input("\nPress Enter to close this window...")
    except Exception:
        pass


def main():
    print("gblink launcher")
    net = find_net()
    if net is None:
        print("\nI cannot find the 'net' folder (net\\pannello.py) next to gblink.exe.")
        print("Unzip the whole package and keep gblink.exe next to the 'net' folder.")
        pause()
        return 1

    py = find_python()
    if py is None:
        print("\nPython 3.8 or newer was not found on this PC.")
        print("Install it from https://www.python.org/downloads/ and TICK")
        print("'Add python.exe to PATH' in the installer, then run this again.")
        try:
            webbrowser.open("https://www.python.org/downloads/")
        except Exception:
            pass
        pause()
        return 1
    print("Python: " + " ".join(py))

    code, _ = run_quiet(py + ["-c", NEEDED], 30)
    if code != 0:
        print("Installing the USB libraries (pyusb, libusb-package), once...")
        code, out = run_quiet(py + ["-m", "pip", "install", "--user", "pyusb", "libusb-package"], 300)
        if code != 0:
            print(out[-1500:])
            print("\nCould not install them automatically. Run this yourself and try again:")
            print("  " + " ".join(py) + " -m pip install pyusb libusb-package")
            print("(The panel still opens, but the USB adapter will not work without them.)")

    if not os.path.exists(os.path.join(net, "mbstub.gba")):
        print("\nNote: net\\mbstub.gba is missing. Copy it into the net folder to use")
        print("'Load the game into the GBA'.")

    print("\nStarting the panel (leave this window open)...\n")
    try:
        rc = subprocess.call(py + ["-u", "pannello.py"], cwd=net)
    except KeyboardInterrupt:
        rc = 0
    if rc != 0:
        print("\nThe panel stopped with an error (code %s)." % rc)
        pause()
    return rc


if __name__ == "__main__":
    sys.exit(main())
