"""Independent local Monitor service and desktop browser launcher; no capture."""
import argparse
import os
from pathlib import Path
import sys
import time
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parents[2] / "Baymax-main"
sys.path.insert(0, str(ROOT))
URL = "http://127.0.0.1:8766/monitor"


def display_connected(drm=Path("/sys/class/drm")):
    # A remote desktop DISPLAY alone does not indicate an attached physical screen.
    for status in drm.glob("card*-*/status"):
        try:
            if status.read_text().strip() == "connected":
                return True
        except OSError:
            continue
    return False


def open_monitor(automatic=False):
    if automatic and (not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")) or
                      not display_connected()):
        return 0
    for _ in range(60):
        try:
            with urllib.request.urlopen(URL, timeout=1) as response:
                if b"<title>Ember" in response.read(65536):
                    return 0 if webbrowser.open(URL) else 1
        except OSError:
            pass
        time.sleep(0.5)
    print("Monitor is not ready. Check: journalctl --user -u ember-monitor.service -b", file=sys.stderr)
    return 1


def serve():
    os.execv(sys.executable, [sys.executable, str(ROOT / "devUI.py"), "--monitor", "--no-browser"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--serve", action="store_true")
    mode.add_argument("--open", action="store_true")
    mode.add_argument("--auto-open", action="store_true")
    args = parser.parse_args()
    return serve() if args.serve else open_monitor(args.auto_open)


if __name__ == "__main__":
    sys.exit(main())
