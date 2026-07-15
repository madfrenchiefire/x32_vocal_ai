"""Desktop entrypoint for the packaged (.exe) build.

Starts the local web server and opens the default browser at the UI. This
is the PyInstaller entry script (see x32vocal.spec) -- end users just
double-click the .exe; the console window shows startup/license/errors and
the app itself lives in the browser tab that opens.

Running from source is identical to `python -m app.main`, just with the
browser auto-open:

    python launcher.py
"""
from __future__ import annotations

import sys
import threading
import webbrowser

from app.config import load_config
from app.main import main

BROWSER_OPEN_DELAY_SEC = 2.0


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        # Headless / no default browser -- the URL is printed anyway.
        pass


def run() -> int:
    # config.json sits next to the .exe (or the cwd when run from source).
    config = load_config("config.json")
    url = f"http://{config.web_host}:{config.web_port}"
    print(f"X32 Vocal AI is starting. Open {url} if the browser doesn't.")
    threading.Timer(BROWSER_OPEN_DELAY_SEC, _open_browser, args=[url]).start()
    return main([])


if __name__ == "__main__":
    sys.exit(run())
