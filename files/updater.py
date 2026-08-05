"""
Updates this app from the internet.

The client runs the app on their own machine; nothing about it is hosted.
This is the one thing that reaches out: on launch it asks a single URL whether
a newer version of the CODE exists, and if so replaces the code files before
the app starts. Their records are never involved.

What it will and won't touch
----------------------------
Replaced : the shared program files (app.py, db.py, templates, stylesheet...)
Never    : data/app.db          -- their records
           modules.py           -- THIS business's tabs and fields
           anything not listed in the manifest

Safety rules, in order:
  1. No URL configured -> it does nothing at all and never touches the network.
  2. HTTPS only. A plain-http manifest is refused outright -- anyone on the
     same wifi could otherwise swap the payload for their own code.
  3. Every file's sha256 must match the manifest before ANYTHING is written,
     so a half-finished download can't leave a broken app behind.
  4. The files being replaced are copied into backup/<old-version>/ first.
  5. Any failure = give up quietly and run the version already installed.
     A broken update must never stop someone opening their own business app.

Honest limit: the hash list proves the download wasn't corrupted or tampered
with in transit, not that whoever published it was who they claim. Whoever
controls that URL can run code on this machine -- keep the account locked down.
"""

from __future__ import annotations

import os
import json
import shutil
import hashlib
import tempfile
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
MANIFEST_URL = "https://raw.githubusercontent.com/jtiztiz0-sudo/Corder-updates/main/manifest.json"          # blank = updates switched off
VERSION_FILE = os.path.join(HERE, "version.txt")
LOG_FILE = os.path.join(HERE, "update.log")

CONNECT_TIMEOUT = 8          # seconds -- a dead URL must not delay the launch
MAX_BYTES = 5 * 1024 * 1024  # a whole app is well under this; refuse anything huge


def _log(msg: str) -> None:
    try:
        from datetime import datetime
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def _url_allowed(url: str) -> bool:
    """HTTPS only -- over plain http anyone sharing the client's wifi could
    swap the payload for their own code. Loopback is the one exception: that
    traffic never leaves the machine, and it's what the tests run against."""
    low = (url or "").lower()
    if low.startswith("https://"):
        return True
    return low.startswith("http://127.0.0.1") or low.startswith("http://localhost")


def local_version() -> int:
    try:
        with open(VERSION_FILE, encoding="utf-8") as fh:
            return int((fh.read() or "0").strip() or 0)
    except (OSError, ValueError):
        return 0


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "corder-app-updater"})
    with urllib.request.urlopen(req, timeout=CONNECT_TIMEOUT) as resp:
        data = resp.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file bigger than expected -- refusing it")
    return data


def check():
    """Is there a newer version? Returns the manifest, or None."""
    if not MANIFEST_URL:
        return None
    if not _url_allowed(MANIFEST_URL):
        _log("refused: update URL is not https")
        return None
    try:
        manifest = json.loads(_get(MANIFEST_URL).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log("check failed: %s" % exc)
        return None
    try:
        if int(manifest.get("version", 0)) <= local_version():
            return None
    except (TypeError, ValueError):
        return None
    if not isinstance(manifest.get("files"), dict) or not manifest["files"]:
        _log("manifest has no files -- ignoring")
        return None
    return manifest


def _safe_relpath(rel: str) -> str | None:
    r"""Reject anything that would write outside this folder (../, C:\..., /etc)."""
    rel = rel.replace("\\", "/")
    if rel.startswith("/") or ".." in rel.split("/") or ":" in rel:
        return None
    return os.path.normpath(os.path.join(HERE, *rel.split("/")))


def apply(manifest) -> bool:
    """Download everything, verify it, then swap it in. All or nothing."""
    base = manifest.get("base") or MANIFEST_URL.rsplit("/", 1)[0] + "/files/"
    staged = {}
    tmp = tempfile.mkdtemp(prefix="app-update-")
    try:
        for rel, meta in manifest["files"].items():
            dest = _safe_relpath(rel)
            if dest is None:
                _log("refused suspicious path in manifest: %s" % rel)
                return False
            # A file name can legally contain spaces ("OPEN-THE-APP.bat"
            # does) and a raw space is not valid in a URL -- urllib rejects
            # it outright and the whole update fails, silently. Quote it,
            # keeping the "/" separators intact.
            blob = _get(base + urllib.parse.quote(rel))
            want = (meta or {}).get("sha256", "")
            got = hashlib.sha256(blob).hexdigest()
            if want != got:
                _log("checksum mismatch on %s -- update abandoned" % rel)
                return False
            hold = os.path.join(tmp, rel.replace("/", "__"))
            with open(hold, "wb") as fh:
                fh.write(blob)
            staged[dest] = hold

        # everything downloaded and verified -- now it's safe to write
        backup = os.path.join(HERE, "backup", str(local_version()))
        for dest, hold in staged.items():
            rel = os.path.relpath(dest, HERE)
            if os.path.exists(dest):
                keep = os.path.join(backup, rel)
                os.makedirs(os.path.dirname(keep), exist_ok=True)
                shutil.copy2(dest, keep)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.copy2(hold, dest)

        with open(VERSION_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(int(manifest["version"])))
        _log("updated to version %s (%d files)" % (manifest["version"], len(staged)))
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _log("update failed, keeping the version already installed: %s" % exc)
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def update_if_available() -> bool:
    """Called on launch, before the app loads. True = files changed."""
    manifest = check()
    if not manifest:
        return False
    return apply(manifest)


if __name__ == "__main__":
    print("installed version:", local_version())
    m = check()
    print("update available:", m["version"] if m else "no")
    if m and input("install it? [y/N] ").strip().lower() == "y":
        print("updated" if apply(m) else "failed -- see update.log")
