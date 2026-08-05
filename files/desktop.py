"""
Double-click launcher: starts the app and opens it in its own window.

Run with pythonw.exe (or run.bat) for no console window. Picks whatever port
is free, so several of these can run at once without colliding.

When CorderHome launches this, it passes GENAPP_X/GENAPP_Y so the window
opens on the same monitor as the button that was clicked -- otherwise the
window manager drops it on the primary screen, which is the wrong one if
you're working on a second display. Double-clicked directly, those aren't
set and the window lands wherever the system puts it.
"""

from __future__ import annotations

import os
import sys
import time
import socket
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Before anything else: if this is the copy someone just unzipped, install it
# somewhere permanent, make a Desktop icon, and reopen from there. Runs once.
# Deliberately ahead of the update check -- no point updating a folder we're
# about to move out of.
try:
    import firstrun
    import modules as _m
    _home = firstrun.ensure_installed(os.path.dirname(os.path.abspath(__file__)),
                                      (_m.BUSINESS or {}).get("name", "App"))
    if _home and firstrun.relaunch(_home):
        raise SystemExit(0)
except SystemExit:
    raise
except Exception:
    pass          # never let setup stand between someone and their own records

try:
    import updater
    # Check for a new version BEFORE the app is imported, so an update takes
    # effect on this launch -- no "restart to finish updating" step. Does
    # nothing at all if no update URL was configured, and gives up quietly on
    # any problem rather than standing between someone and their own records.
    updater.update_if_available()
except Exception:
    pass

from app import app          # noqa: E402
import modules               # noqa: E402


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


PORT = _free_port()
URL = "http://127.0.0.1:%d" % PORT


def _serve() -> None:
    # no reloader: it would spawn a second process and break the window
    app.run(host="127.0.0.1", port=PORT, debug=False,
            use_reloader=False, threaded=True)


def _wait_until_up(timeout: float = 10.0) -> None:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                return
        except OSError:
            time.sleep(0.15)


def _start_pos():
    """Where to put the window, if we were told. None = let the system pick."""
    try:
        return int(os.environ["GENAPP_X"]), int(os.environ["GENAPP_Y"])
    except (KeyError, ValueError):
        return None, None


def main() -> None:
    threading.Thread(target=_serve, daemon=True).start()
    _wait_until_up()
    title = modules.BUSINESS.get("name") or "My Business"
    start_x, start_y = _start_pos()
    try:
        import webview
        webview.create_window(title, URL, width=1180, height=820,
                              min_size=(900, 560), x=start_x, y=start_y)
        webview.start()      # blocks until the window is closed
    except Exception:
        import webbrowser
        webbrowser.open(URL)
        print("%s running at %s — close this window to stop." % (title, URL))
        while True:
            time.sleep(3600)


if __name__ == "__main__":
    main()
