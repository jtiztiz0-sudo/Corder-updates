"""
Writes down what went wrong, so a phone call isn't the only evidence.

These apps run with no console window. Without this, an error vanishes the
instant it happens and "it stopped working" is all anyone has to go on.

WHAT GOES IN:  the time, which page, the error, and where in the code.
WHAT NEVER GOES IN:  anything they typed. No form values, no row contents, no
    customer names. A log that leaks a business's records is worse than no log,
    because this file is meant to be emailed to someone else.

One JSON object per line, so it can be read back exactly rather than guessed
at with a regex. The file is capped -- an app left running for years must not
fill a disk with its own complaints.
"""

from __future__ import annotations

import os
import json
import traceback
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
LOG_FILE = os.path.join(LOG_DIR, "app.log")
MAX_LINES = 400


def _trim() -> None:
    """Keep the newest MAX_LINES. Only ever touches our own file."""
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > MAX_LINES:
            with open(LOG_FILE, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-MAX_LINES:])
    except OSError:
        pass


def log(kind: str, where: str = "", error: str = "", trace: str = "") -> None:
    """Record one event. Never raises -- a broken logger must not be the thing
    that takes the app down."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        row = {"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "kind": kind, "where": where or "", "error": error or "",
               "trace": (trace or "")[-2000:]}
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        _trim()
    except Exception:
        pass


def log_exception(exc: BaseException, where: str = "") -> None:
    log("error", where, "%s: %s" % (type(exc).__name__, exc),
        "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))


def read(limit: int = 200) -> list:
    """Everything recorded, newest first."""
    out = []
    try:
        with open(LOG_FILE, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return list(reversed(out))[:limit]


def export(dest_dir: str = "") -> str:
    """Zip the logs -- and ONLY the logs -- somewhere they can find them.

    Their records are deliberately not in here. This file gets emailed; it has
    to be safe to send.
    """
    import zipfile
    dest_dir = dest_dir or os.path.join(os.path.expanduser("~"), "Desktop")
    if not os.path.isdir(dest_dir):
        dest_dir = os.path.expanduser("~")
    name = "problem-report-%s.zip" % datetime.now().strftime("%Y%m%d-%H%M")
    out = os.path.join(dest_dir, name)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in ("logs/app.log", "update.log", "version.txt", "app_version.txt"):
            p = os.path.join(HERE, *rel.split("/"))
            if os.path.isfile(p):
                z.write(p, rel)
        try:
            import modules
            z.writestr("about.txt", "%s\nid: %s\nbuilt: %s\n" % (
                modules.BUSINESS.get("name", ""), modules.BUSINESS.get("id", ""),
                modules.BUSINESS.get("generated", "")))
        except Exception:
            pass
    return out
