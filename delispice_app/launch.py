"""Launch delispice_app in a native pop-up window (pywebview around the Dash server).

Run from the repo root:  ``python -m delispice_app.launch``   (or ``python delispice_app/launch.py``)

Starts the Dash server on a local port in a background thread, waits for it to answer, then opens a
native macOS window (WebKit) pointed at it. Closing the window exits the program.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # ensure repo root is importable

import webview                                                  # noqa: E402

from delispice_app.app import app                                 # noqa: E402  (import builds the index once)

HOST = "127.0.0.1"


def _pick_port(preferred: int) -> int:
    with socket.socket() as s:
        try:
            s.bind((HOST, preferred))
            return preferred
        except OSError:
            with socket.socket() as s2:
                s2.bind((HOST, 0))
                return s2.getsockname()[1]


def _wait_until_up(url: str, timeout: float = 90.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.25)
    return False


def main() -> None:
    port = _pick_port(int(os.environ.get("PITCHER_PORT", "8765")))
    url = f"http://{HOST}:{port}/"
    threading.Thread(target=lambda: app.run(host=HOST, port=port, debug=False, use_reloader=False),
                     daemon=True).start()
    if not _wait_until_up(url):
        print(f"Server did not come up at {url}", file=sys.stderr)
        sys.exit(1)
    # Open maximized (fills the screen, still a normal window — not macOS fullscreen).
    # width/height are the size it returns to if you un-maximize.
    webview.create_window("delispice_app", url, width=1280, height=900, maximized=True)
    webview.start()


if __name__ == "__main__":
    main()
